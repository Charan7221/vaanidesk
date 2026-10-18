"""Check that your API keys and providers work, one by one.

    python -m scripts.check_setup

Tests: LLM (with tool calling) -> Sarvam TTS (stream + REST) -> Sarvam/Deepgram STT
(feeds the generated speech back into speech-to-text). Prints PASS/FAIL with hints.
"""
from __future__ import annotations

import asyncio
import sys
import time

from app.config import get_settings
from app.providers import build_llm, build_stt
from app.providers.tts_sarvam import SarvamTTS
from app.tools.registry import TOOL_SCHEMAS

OK, BAD = "PASS", "FAIL"


def report(name: str, ok: bool, detail: str) -> None:
    print(f"[{OK if ok else BAD}] {name:<22} {detail}")


async def check_llm(s) -> None:
    llm = build_llm(s)
    t0 = time.perf_counter()
    text, tools = [], []
    try:
        async for c in llm.stream([
            {"role": "system", "content": "You are a clinic receptionist. Use tools to check availability."},
            {"role": "user", "content": "Do you have any slots on 2026-12-14 in the evening?"},
        ], TOOL_SCHEMAS):
            text.append(c.text)
            tools += c.tool_calls
        ms = round((time.perf_counter() - t0) * 1000)
        if tools:
            report("LLM + tool calling", True, f"{s.openai_model}: called {tools[0].name}({tools[0].arguments}) in {ms} ms")
        else:
            report("LLM + tool calling", False, f"{s.openai_model} answered without calling a tool: {''.join(text)[:80]!r}"
                   " (works, but booking may be unreliable; try a stronger model)")
    except Exception as exc:
        report("LLM", False, f"{type(exc).__name__}: {str(exc)[:300]}")
        print("      hint: check OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL in .env")


async def check_tts(s, mode: str) -> bytes:
    tts = SarvamTTS(s.sarvam_api_key, s.sarvam_tts_model, s.sarvam_tts_speaker, s.sarvam_tts_pace, mode)
    t0 = time.perf_counter()
    audio, first = bytearray(), None
    try:
        stream = await tts.open("te-IN", 16000)
        await stream.send_text("నమస్కారం, నాకు రేపు సాయంత్రం అపాయింట్‌మెంట్ కావాలి.")
        await stream.finish()

        async def collect():
            nonlocal first
            async for chunk in stream.audio():
                first = first or time.perf_counter()
                audio.extend(chunk)

        await asyncio.wait_for(collect(), timeout=20)
        await stream.close()
    except Exception as exc:
        report(f"Sarvam TTS ({mode})", False, f"{type(exc).__name__}: {str(exc)[:300]}")
        return b""
    if audio:
        report(f"Sarvam TTS ({mode})", True, f"{len(audio) / 32000:.1f}s of audio, first audio after "
               f"{round((first - t0) * 1000)} ms")
    else:
        report(f"Sarvam TTS ({mode})", False, "no audio returned (see log lines above)")
    return bytes(audio)


async def check_stt(s, audio: bytes) -> None:
    name = f"STT ({s.stt_provider})"
    try:
        stream = await build_stt(s).open(16000)
    except Exception as exc:
        report(name, False, f"could not connect: {type(exc).__name__}: {str(exc)[:300]}")
        return
    got = []

    async def reader():
        async for ev in stream.events():
            got.append(ev)
            if ev.kind == "final":
                return

    task = asyncio.create_task(reader())
    for i in range(0, len(audio), 3200):  # stream in real time-ish (100 ms chunks)
        await stream.send_audio(audio[i:i + 3200])
        await asyncio.sleep(0.05)
    for _ in range(30):  # 1.5 s of silence so the end of speech is detected
        await stream.send_audio(b"\x00\x00" * 1600)
        await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(task, timeout=10)
    except TimeoutError:
        pass
    await stream.close()
    finals = [e for e in got if e.kind == "final"]
    errors = [e for e in got if e.kind == "error"]
    if finals:
        report(name, True, f"heard: {finals[0].text!r} (language {finals[0].language})")
    else:
        report(name, False, f"no transcript. events={[e.kind for e in got]} errors={[e.text for e in errors]}")


async def check_google_calendar(s) -> None:
    if not s.google_calendar_enabled:
        print("[SKIP] Google Calendar          (GOOGLE_CALENDAR_ID / service account not set)")
        return
    import json

    from app.business import load_business
    from app.db import Database
    from app.tools.calendar import GoogleCalendarSync

    if s.google_calendar_id.strip().endswith(".iam.gserviceaccount.com"):
        report("Google Calendar", False, "GOOGLE_CALENDAR_ID is the service account's email, not your calendar's ID")
        print("      hint: Google Calendar -> your clinic calendar -> Settings -> Integrate calendar -> copy 'Calendar ID'")
        print("            (it ends with @group.calendar.google.com, or is your Gmail address for your main calendar)")
        return
    info = json.loads(s.google_service_account_json) if s.google_service_account_json else None
    try:
        cal = GoogleCalendarSync(Database("sqlite+aiosqlite://"), load_business(s.business_config_path),
                                 s.google_calendar_id, service_account_file=s.google_service_account_file,
                                 service_account_info=info)
    except Exception as exc:
        report("Google Calendar", False, f"could not load service account key: {type(exc).__name__}: {exc}")
        return
    try:
        name = await cal.verify()
        report("Google Calendar", True, f"connected to calendar '{name}' as {cal.service_account_email}")
    except Exception as exc:
        report("Google Calendar", False, str(exc)[:300])
        print(f"      hint: share the calendar with {cal.service_account_email} "
              "(Settings -> Share with specific people -> 'Make changes to events'),")
        print("            and enable the Google Calendar API in your Google Cloud project.")


async def main() -> None:
    s = get_settings()
    print(f"Config: LLM={s.openai_model} @ {s.openai_base_url or 'OpenAI'} | STT={s.stt_provider} | "
          f"TTS={s.sarvam_tts_model}/{s.sarvam_tts_speaker} mode={s.tts_mode}\n")
    if "--google" in sys.argv:  # only test the calendar
        await check_google_calendar(s)
        return
    if s.missing_keys():
        print("Missing keys in .env:", ", ".join(s.missing_keys()))
        sys.exit(1)
    await check_llm(s)
    audio = await check_tts(s, "stream")
    rest_audio = await check_tts(s, "rest")
    if not audio and rest_audio:
        print("      hint: streaming TTS failed but REST works -> set TTS_MODE=rest in .env")
    if audio or rest_audio:
        await check_stt(s, audio or rest_audio)
    else:
        print("[SKIP] STT                     (needs TTS audio to test with)")
    await check_google_calendar(s)


if __name__ == "__main__":
    asyncio.run(main())
