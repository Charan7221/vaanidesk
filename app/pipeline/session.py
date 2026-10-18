"""The real-time conversation engine.

Pipeline per caller turn:

    caller audio -> STT (streaming) -> end-of-turn detection -> LLM (streaming, tools)
                 -> sentence chunker -> TTS (streaming) -> caller

Key behaviours:
  * Barge-in: if the caller starts speaking while the agent is talking, the current
    response is cancelled and buffered playback is cleared immediately.
  * Low latency: speech starts as soon as the first sentence of the LLM reply is ready.
  * Multilingual: TTS language follows the script of each reply (English/Hindi/Telugu).
  * Metrics: end-of-speech -> first-audio latency is measured for every turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass

from ..audio import pcm16_duration_ms
from ..business import Business
from ..db import Database
from ..providers.base import LLMProvider, STTEvent, STTProvider, TTSProvider, TTSStream
from ..tools.registry import TOOL_SCHEMAS, ToolContext, ToolExecutor
from .prompts import (
    NO_FILLER_TOOLS,
    ControlNoteFilter,
    filler,
    greeting_segments,
    language_rule,
    strip_control_notes,
    system_prompt,
)
from .text import SentenceChunker, detect_script_language, looks_unfinished

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4

# English words Indian callers commonly use inside Hindi/Telugu speech; never a language switch.
_LOAN_WORDS = {
    "doctor", "dr", "appointment", "ok", "okay", "yes", "no", "sir", "madam", "please", "thanks", "thank",
    "you", "hello", "hi", "sorry", "time", "slot", "morning", "evening", "afternoon", "tomorrow", "today",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "am", "pm", "book",
    "booking", "cancel", "number", "phone", "mobile", "name", "is", "the", "a", "to", "for", "with", "and",
    "garu", "ji", "haan", "ha", "na", "fine", "confirm", "done",
}


@dataclass
class SessionDeps:
    business: Business
    db: Database
    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    tools: ToolExecutor
    end_of_turn_delay_ms: int = 350
    incomplete_turn_delay_ms: int = 1200  # caller paused mid-sentence ("I want to book for...")
    max_call_seconds: int = 600
    summarise: bool = True  # write an AI summary of the call for the dashboard


class CallSession:
    def __init__(self, deps: SessionDeps, transport, ctx: ToolContext):
        self.d = deps
        self.t = transport
        self.ctx = ctx
        self._base_prompt = system_prompt(deps.business, caller_phone=ctx.caller_phone)
        biz = deps.business
        names = " ".join([biz.name, biz.assistant_name] + [f"{p.name} {p.id} {p.specialty}" for p in biz.providers])
        self._ignore_words = set(re.findall(r"[^\W\d_]+", names.lower())) | _LOAN_WORDS
        self.messages: list[dict] = [
            {"role": "system", "content": self._base_prompt + language_rule(ctx.language)}
        ]
        self.stt_stream = None
        self._tasks: list[asyncio.Task] = []
        self._response: asyncio.Task | None = None
        self._turn_timer: asyncio.Task | None = None
        self._user_buf: list[str] = []
        self._turn_end_at: float | None = None
        self._playback_until = 0.0  # monotonic time when queued audio finishes playing
        self._latencies: list[float] = []
        self._first_audio_at = 0.0
        self.interruptions = 0
        self.closed = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            self.stt_stream = await self.d.stt.open(self.t.sample_rate)
        except Exception as exc:
            raise RuntimeError(f"Could not connect to speech-to-text ({type(self.d.stt).__name__}): {exc}") from exc
        self._tasks.append(asyncio.create_task(self._stt_loop()))
        self._tasks.append(asyncio.create_task(self._watchdog()))
        segments = greeting_segments(self.d.business, self.ctx.language)
        text = " ".join(t for _, t in segments)
        self.messages.append({"role": "assistant", "content": text})
        await self.d.db.add_turn(self.ctx.call_id, "assistant", text, language=self.ctx.language)
        await self.t.send_event({"type": "assistant", "text": text, "final": True})
        self._response = asyncio.create_task(self._speak_segments(segments))

    async def feed_audio(self, pcm16: bytes) -> None:
        if self.stt_stream and not self.closed.is_set():
            await self.stt_stream.send_audio(pcm16)

    async def feed_text(self, text: str) -> None:
        """Typed input (browser demo): behaves like a finished spoken utterance."""
        await self._on_stt_event(STTEvent("speech_started"))
        await self._on_stt_event(STTEvent("final", text=text))
        self._schedule_end_of_turn(delay_ms=0)

    async def close(self, status: str = "completed") -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        current = asyncio.current_task()
        for task in [self._response, self._turn_timer, *self._tasks]:
            if task and task is not current and not task.done():
                task.cancel()
        if self.stt_stream:
            await self.stt_stream.close()
        avg = sum(self._latencies) / len(self._latencies) if self._latencies else None
        if self.ctx.outcome == "transferred":
            status = "transferred"
        await self.d.db.finish_call(
            self.ctx.call_id, status=status, outcome=self.ctx.outcome or "info", language=self.ctx.language,
            avg_latency_ms=avg, turns_count=len(self._latencies), interruptions=self.interruptions,
        )
        if self.d.summarise:
            asyncio.create_task(self._summarise())

    # ------------------------------------------------------------------ STT handling
    async def _stt_loop(self) -> None:
        try:
            async for ev in self.stt_stream.events():
                await self._on_stt_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("STT loop crashed")

    @property
    def agent_busy(self) -> bool:
        responding = self._response is not None and not self._response.done()
        return responding or time.monotonic() < self._playback_until

    async def _on_stt_event(self, ev: STTEvent) -> None:
        if ev.kind == "speech_started":
            if self._turn_timer and not self._turn_timer.done():
                self._turn_timer.cancel()  # caller is still talking: keep collecting
            if self.agent_busy:
                await self._barge_in()
        elif ev.kind == "interim":
            await self.t.send_event({"type": "user", "text": " ".join(self._user_buf + [ev.text]), "final": False})
            if self.agent_busy and len(ev.text.split()) >= 2:
                await self._barge_in()
        elif ev.kind == "final":
            self._user_buf.append(ev.text)
            self._maybe_switch_language(ev.text, ev.language)
            await self.t.send_event({"type": "user", "text": " ".join(self._user_buf), "final": False})
        elif ev.kind == "utterance_end":
            if self._user_buf:
                incomplete = looks_unfinished(" ".join(self._user_buf))
                self._schedule_end_of_turn(self.d.incomplete_turn_delay_ms if incomplete else None)
        elif ev.kind == "error":
            log.warning("STT error: %s", ev.text)

    #: a language switch needs a real sentence, not a name / "ok" / "yes". Switching *to English*
    #: needs more evidence, because Indian callers say names and English words ("doctor",
    #: "appointment", "ok") inside Hindi/Telugu sentences and STT writes those in Latin letters.
    MIN_WORDS_TO_SWITCH = 3
    MIN_WORDS_TO_SWITCH_TO_ENGLISH = 5

    def _maybe_switch_language(self, text: str, detected: str | None) -> None:
        """Keep one call language; switch only when the caller clearly speaks another one.

        Speech-to-text guesses the language of every utterance separately, and short
        utterances (a name like "Raju", "okay", "haan") are often mislabelled. So we
        switch only when the utterance has enough words AND is written in that
        language's script.
        """
        lang = detected or detect_script_language(text, self.ctx.language)
        if lang == self.ctx.language or lang not in self.d.business.languages:
            return
        if detect_script_language(text, self.ctx.language) != lang:
            return
        # Count only "real" words: drop doctor / clinic names and common loan words.
        words = [w for w in re.findall(r"[^\W\d_]+", text.lower()) if w not in self._ignore_words]
        needed = self.MIN_WORDS_TO_SWITCH_TO_ENGLISH if lang == "en-IN" else self.MIN_WORDS_TO_SWITCH
        if len(words) < needed:
            return
        log.info("[%s] language switch %s -> %s", self.ctx.call_id, self.ctx.language, lang)
        self.ctx.language = lang

    def _schedule_end_of_turn(self, delay_ms: int | None = None) -> None:
        if self._turn_timer and not self._turn_timer.done():
            self._turn_timer.cancel()
        self._turn_end_at = time.monotonic()
        delay = (self.d.end_of_turn_delay_ms if delay_ms is None else delay_ms) / 1000
        self._turn_timer = asyncio.create_task(self._end_of_turn(delay))

    async def _end_of_turn(self, delay: float) -> None:
        await asyncio.sleep(delay)
        text = " ".join(self._user_buf).strip()
        self._user_buf = []
        if not text:
            return
        await self.t.send_event({"type": "user", "text": text, "final": True})
        await self.d.db.add_turn(self.ctx.call_id, "user", text, language=self.ctx.language)
        if self._response and not self._response.done():
            self._response.cancel()
        self._response = asyncio.create_task(self._respond(text))

    async def _barge_in(self) -> None:
        self.interruptions += 1
        if self._response and not self._response.done():
            self._response.cancel()
        self._playback_until = 0.0
        await self.t.clear_audio()
        await self.t.send_event({"type": "interrupted"})
        log.info("[%s] barge-in", self.ctx.call_id)

    # ------------------------------------------------------------------ response
    async def _play(self, tts: TTSStream, first_audio: asyncio.Event) -> None:
        async for pcm in tts.audio():
            now = time.monotonic()
            if not first_audio.is_set():
                self._first_audio_at = now
                first_audio.set()
            self._playback_until = max(self._playback_until, now) + pcm16_duration_ms(pcm, self.t.sample_rate) / 1000
            await self.t.send_audio(pcm)

    async def _speak_segments(self, segments: list[tuple[str, str]]) -> None:
        """Speak each (language, text) piece with a voice for that language (e.g. a multilingual greeting)."""
        for lang, text in segments:
            tts = await self.d.tts.open(lang, self.t.sample_rate)
            try:
                player = asyncio.create_task(self._play(tts, asyncio.Event()))
                await tts.send_text(text)
                await tts.finish()
                await player
            finally:
                await tts.close()

    async def _respond(self, user_text: str) -> None:
        t0 = self._turn_end_at or time.monotonic()
        self._repair_history()
        # Tell the model the reply language in its instructions (not in the caller's message,
        # where models sometimes repeat it out loud).
        self.messages[0]["content"] = self._base_prompt + language_rule(self.ctx.language)
        self.messages.append({"role": "user", "content": user_text})
        chunker = SentenceChunker()
        notes = ControlNoteFilter()
        tts: TTSStream | None = None
        player: asyncio.Task | None = None
        first_audio = asyncio.Event()
        spoken: list[str] = []
        # Connect to TTS while the LLM is thinking, so the first sentence isn't delayed by the handshake.
        call_lang = self.ctx.language
        warm_tts: asyncio.Task | None = asyncio.create_task(self.d.tts.open(call_lang, self.t.sample_rate))

        async def speak(sentence: str) -> None:
            nonlocal tts, player, warm_tts
            sentence = strip_control_notes(sentence)
            if not sentence:
                return
            if tts is None:
                lang = detect_script_language(sentence, call_lang)
                if warm_tts is not None and lang == call_lang:
                    tts, warm_tts = await warm_tts, None
                else:
                    tts = await self.d.tts.open(lang, self.t.sample_rate)
                player = asyncio.create_task(self._play(tts, first_audio))
            spoken.append(sentence)
            await self.t.send_event({"type": "assistant", "text": " ".join(spoken), "final": False})
            await tts.send_text(sentence + " ")

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                text_parts: list[str] = []
                tool_calls = []
                async for chunk in self.d.llm.stream(self.messages, TOOL_SCHEMAS):
                    if chunk.text:
                        text_parts.append(chunk.text)
                        for sentence in chunker.push(notes.push(chunk.text)):
                            await speak(sentence)
                    if chunk.tool_calls:
                        tool_calls.extend(chunk.tool_calls)
                tail = notes.flush()
                for sentence in chunker.push(tail) if tail else []:
                    await speak(sentence)
                rest = chunker.flush()
                if rest:
                    await speak(rest)

                content = strip_control_notes("".join(text_parts)) or None
                if tool_calls and not spoken and not all(tc.name in NO_FILLER_TOOLS for tc in tool_calls):
                    # Nothing said yet and a tool is about to run: fill the silence immediately.
                    content = filler(self.ctx.language)
                    await speak(content)
                assistant_msg: dict = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {**tc.extra, "id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                        for tc in tool_calls
                    ]
                self.messages.append(assistant_msg)
                if not tool_calls:
                    break

                for tc in tool_calls:
                    result = await self.d.tools.run(self.ctx, tc.name, tc.arguments)
                    log.info("[%s] tool %s(%s) -> %s", self.ctx.call_id, tc.name, tc.arguments, result)
                    await self.t.send_event({"type": "tool", "name": tc.name, "args": tc.arguments, "result": result})
                    await self.d.db.add_turn(self.ctx.call_id, "tool", f"{tc.name}", meta={"args": tc.arguments,
                                                                                         "result": result})
                    self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                          "content": json.dumps(result, ensure_ascii=False, default=str)})

            if tts is not None:
                await tts.finish()
                # Wait for the first audio so latency is measured, then let playback stream out.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(first_audio.wait(), timeout=10)
                latency_ms = (self._first_audio_at - t0) * 1000 if first_audio.is_set() else None
                if latency_ms is not None:
                    self._latencies.append(latency_ms)
                    await self.t.send_event({"type": "metrics", "latency_ms": round(latency_ms)})
                if player:
                    await player
            else:
                latency_ms = None

            reply = " ".join(spoken)
            if reply:
                await self.t.send_event({"type": "assistant", "text": reply, "final": True})
                await self.d.db.add_turn(self.ctx.call_id, "assistant", reply, language=self.ctx.language,
                                         latency_ms=latency_ms)
            await self._run_pending_action()
        except asyncio.CancelledError:
            # Barge-in: remember what was actually said so the LLM has the right context.
            if spoken:
                self.messages.append({"role": "assistant", "content": " ".join(spoken) + " [interrupted by caller]"})
                await self.d.db.add_turn(self.ctx.call_id, "assistant", " ".join(spoken),
                                         language=self.ctx.language, meta={"interrupted": True})
            raise
        except Exception as exc:  # provider outage etc. — keep the call alive
            log.exception("[%s] response failed", self.ctx.call_id)
            await self.t.send_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if player and not player.done():
                player.cancel()
            if tts is not None:
                await tts.close()
            if warm_tts is not None:  # pre-opened but never used (e.g. reply in another language)
                warm_tts.cancel()
                with contextlib.suppress(BaseException):
                    await (await warm_tts).close()

    def _repair_history(self) -> None:
        """Make sure every assistant tool call has a tool result (a barge-in can cut a round short)."""
        answered = {m.get("tool_call_id") for m in self.messages if m["role"] == "tool"}
        repaired: list[dict] = []
        for msg in self.messages:
            repaired.append(msg)
            for tc in msg.get("tool_calls") or []:
                if tc["id"] not in answered:
                    repaired.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps({"error": "interrupted by caller"})})
        # tool results must directly follow their assistant message: rebuild order
        ordered: list[dict] = []
        pending_tools = {m["tool_call_id"]: m for m in repaired if m["role"] == "tool"}
        for msg in repaired:
            if msg["role"] == "tool":
                continue
            ordered.append(msg)
            for tc in msg.get("tool_calls") or []:
                ordered.append(pending_tools[tc["id"]])
        self.messages = ordered

    async def _run_pending_action(self) -> None:
        action = self.ctx.pending_action
        if not action:
            return
        self.ctx.pending_action = None
        # Let the goodbye / "connecting you" sentence finish playing first.
        await asyncio.sleep(max(0.0, self._playback_until - time.monotonic()) + 0.3)
        if action == "transfer":
            await self.t.send_event({"type": "transfer", "number": self.d.business.handoff_number})
            await self.t.transfer(self.d.business.handoff_number)
        elif action == "hangup":
            await self.t.send_event({"type": "hangup"})
            await self.t.hangup()
        await self.close()

    # ------------------------------------------------------------------ housekeeping
    async def _watchdog(self) -> None:
        await asyncio.sleep(self.d.max_call_seconds)
        log.info("[%s] max call duration reached", self.ctx.call_id)
        await self.t.hangup()
        await self.close(status="completed")

    async def _summarise(self) -> None:
        convo = [m for m in self.messages[1:] if m["role"] in ("user", "assistant") and m.get("content")]
        if len(convo) < 2:
            return
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in convo)
        try:
            summary = await self.d.llm.complete([
                {"role": "system", "content": "Summarise this receptionist phone call in one English sentence "
                                              "for the business owner (who called, what they wanted, outcome)."},
                {"role": "user", "content": transcript[-6000:]},
            ])
            await self.d.db.update_call(self.ctx.call_id, summary=summary.strip())
        except Exception as exc:
            log.warning("Summary failed: %s", exc)
