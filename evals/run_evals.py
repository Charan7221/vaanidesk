"""Conversation eval harness for VaaniDesk.

Runs scripted multilingual callers (evals/scenarios.yaml) through the real conversation
engine + LLM + tools, with audio replaced by text, and scores each scenario.

    python -m evals.run_evals                 # all scenarios
    python -m evals.run_evals -k telugu       # filter by name
    python -m evals.run_evals --repeat 3      # run each scenario 3x (measures consistency)

Writes evals/results.json and prints a summary table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from app.business import load_business
from app.config import ROOT_DIR, get_settings
from app.db import Database
from app.pipeline.session import CallSession, SessionDeps
from app.pipeline.text import detect_script_language
from app.providers import build_llm
from app.providers.base import STTEvent, STTProvider, STTStream, TTSProvider, TTSStream
from app.tools.calendar import LocalCalendar
from app.tools.registry import ToolContext, ToolExecutor
from app.tools.twilio_client import TwilioClient
from app.transports.base import Transport

HERE = Path(__file__).parent
CALLER = "+919000011111"


# ----------------------------------------------------------------- text-only stand-ins for audio
class _SilentSTTStream(STTStream):
    def __init__(self):
        self._done = asyncio.Event()

    async def send_audio(self, pcm16: bytes) -> None: ...

    async def events(self) -> AsyncIterator[STTEvent]:
        await self._done.wait()
        if False:
            yield

    async def close(self) -> None:
        self._done.set()


class SilentSTT(STTProvider):
    async def open(self, sample_rate: int, language: str | None = None) -> STTStream:
        return _SilentSTTStream()


class _TextOnlyTTSStream(TTSStream):
    def __init__(self):
        self._done = asyncio.Event()

    async def send_text(self, text: str) -> None: ...

    async def finish(self) -> None:
        self._done.set()

    async def audio(self) -> AsyncIterator[bytes]:
        yield b"\x00\x00"  # one sample so latency is recorded
        await self._done.wait()

    async def close(self) -> None:
        self._done.set()


class TextOnlyTTS(TTSProvider):
    async def open(self, language: str, sample_rate: int) -> TTSStream:
        return _TextOnlyTTSStream()


class RecordingTransport(Transport):
    channel = "eval"
    sample_rate = 16000

    def __init__(self):
        self.events: list[dict] = []

    async def send_audio(self, pcm16: bytes) -> None: ...

    async def clear_audio(self) -> None: ...

    async def send_event(self, event: dict) -> None:
        self.events.append(event)


# ----------------------------------------------------------------- scenario runner
async def send_turn(session: CallSession, text: str, timeout: float = 300.0) -> None:
    """Give the agent one caller turn and wait until the agent's reply to THAT turn has finished.

    The reply task is created a moment after the text arrives (after the turn is saved), so we
    wait for a *new* response task, not whatever response happens to exist. Waiting on the old,
    already-finished one would send the next caller line too early, and the engine would treat it
    as the caller talking over the agent and cancel the reply.
    """
    previous = session._response
    await session.feed_text(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = session._response
        if current is not None and current is not previous and current.done():
            return
        if session.closed.is_set() and (current is None or current.done()):
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"agent did not reply within {timeout:.0f}s")



def _dates(biz) -> dict[str, str]:
    today = biz.now().date()

    def nxt(weekday: int) -> str:
        d = today + timedelta(days=1)
        while d.weekday() != weekday:
            d += timedelta(days=1)
        return d

    mon, sun = nxt(0), nxt(6)
    return {"next_monday": mon.strftime("%A %d %B"), "next_sunday": sun.strftime("%A %d %B"),
            "_monday_date": mon}


async def build_knowledge(settings, tmp_dir: str):
    """The clinic knowledge base, indexed once per eval run (same embedder as production)."""
    from pathlib import Path as _Path

    from app.knowledge import KnowledgeStore, OpenAIEmbedder

    model = settings.resolved_embedding_model
    embedder = OpenAIEmbedder(settings.openai_api_key, model, base_url=settings.openai_base_url or None) \
        if model else None
    db = Database(f"sqlite+aiosqlite:///{tmp_dir}/knowledge.db")
    await db.create_all()
    store = KnowledgeStore(db, embedder, top_k=settings.rag_top_k)
    await store.sync_folder(_Path(settings.knowledge_dir))
    return store


async def run_scenario(sc: dict, llm, settings, knowledge=None) -> dict:
    biz = load_business(settings.business_config_path)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{tmp}/eval.db")
        await db.create_all()
        cal = LocalCalendar(db, biz)
        dates = _dates(biz)
        if seed := sc.get("seed_booking"):
            h, m = map(int, seed["time"].split(":"))
            day = dates["_monday_date"]
            await cal.book(patient_name=seed["patient_name"], phone=CALLER, provider_id=biz.providers[0].id,
                           start=datetime(day.year, day.month, day.day, h, m, tzinfo=biz.tz))
        tools = ToolExecutor(biz, cal, TwilioClient(settings.model_copy(update={"twilio_account_sid": ""})), knowledge)
        call = await db.create_call(channel="eval", caller=CALLER)
        ctx = ToolContext(call_id=call.id, channel="eval", caller_phone=CALLER, language=sc.get("language", "en-IN"))
        deps = SessionDeps(business=biz, db=db, stt=SilentSTT(), llm=llm, tts=TextOnlyTTS(), tools=tools,
                           end_of_turn_delay_ms=0, summarise=False)
        transport = RecordingTransport()
        session = CallSession(deps, transport, ctx)
        await session.start()
        await session._response

        turn_times, first_audio, replies = [], [], []
        for turn in sc["turns"]:
            if session.closed.is_set():
                break
            before = len(transport.events)
            t0 = time.perf_counter()
            await send_turn(session, turn.format(**dates))
            turn_times.append((time.perf_counter() - t0) * 1000)
            first_audio += [e["latency_ms"] for e in transport.events[before:] if e["type"] == "metrics"]
            finals = [e for e in transport.events[before:] if e["type"] == "assistant" and e.get("final")]
            replies.append(finals[-1]["text"] if finals else "")

        tools_called = [e["name"] for e in transport.events if e["type"] == "tool"]
        outcome = ctx.outcome or "info"
        await session.close()
        await db.dispose()

    exp = sc.get("expect", {})
    checks: dict[str, bool] = {}
    for t in exp.get("tools_called", []):
        checks[f"called {t}"] = t in tools_called
    for t in exp.get("tools_not_called", []):
        checks[f"did not call {t}"] = t not in tools_called
    if "outcome" in exp:
        checks[f"outcome={exp['outcome']}"] = outcome == exp["outcome"]
    last = next((r for r in reversed(replies) if r), "")
    if "reply_language" in exp:
        checks[f"reply in {exp['reply_language']}"] = detect_script_language(last) == exp["reply_language"]
    if "max_words" in exp:
        checks[f"replies <= {exp['max_words']} words"] = all(len(r.split()) <= exp["max_words"] for r in replies)
    all_text = " ".join(replies).lower()
    if "contains_any" in exp:
        checks["mentions " + "/".join(exp["contains_any"])] = any(w.lower() in all_text for w in exp["contains_any"])
    for w in exp.get("not_contains", []):
        checks[f"avoids '{w}'"] = w.lower() not in all_text

    return {"name": sc["name"], "passed": all(checks.values()), "checks": checks, "tools_called": tools_called,
            "outcome": outcome, "replies": replies, "turn_ms": [round(t) for t in turn_times],
            "first_audio_ms": [round(t) for t in first_audio]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only run scenarios whose name contains this")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--scenarios", default=str(HERE / "scenarios.yaml"))
    ap.add_argument("--pause", type=float, default=None,
                    help="seconds to wait between scenarios (default: 25 on Gemini free tier, else 0)")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY in .env to run evals.")
    settings = settings.model_copy(update={"business_config_path": settings.business_config_path
                                           or str(ROOT_DIR / "config" / "business.yaml")})
    # Evals are not a live call, so it's fine to wait out rate limits (free tiers: ~15 requests/min).
    llm = build_llm(settings.model_copy(update={"llm_max_retry_wait_s": 65.0}))
    pause = args.pause if args.pause is not None else (25.0 if "generativelanguage" in settings.openai_base_url else 0.0)
    scenarios = [s for s in yaml.safe_load(open(args.scenarios, encoding="utf-8")) if args.k in s["name"]]
    kb_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    knowledge = await build_knowledge(settings, kb_dir.name)

    results = []
    for i, sc in enumerate(scenarios):
        if i and pause:
            await asyncio.sleep(pause)
        for _ in range(args.repeat):
            r = await run_scenario(sc, llm, settings, knowledge)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            failed = [k for k, v in r["checks"].items() if not v]
            print(f"[{mark}] {r['name']:<28} turns(ms)={r['turn_ms']} {('failed: ' + ', '.join(failed)) if failed else ''}")

    passed = sum(r["passed"] for r in results)
    all_turns = [t for r in results for t in r["turn_ms"]]
    all_first = [t for r in results for t in r["first_audio_ms"]]
    summary = {
        "model": settings.openai_model,
        "reasoning_effort": settings.llm_reasoning_effort or "default",
        "runs": len(results),
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "median_turn_ms": round(statistics.median(all_turns)) if all_turns else None,
        "p90_turn_ms": round(statistics.quantiles(all_turns, n=10)[-1]) if len(all_turns) >= 10 else None,
        # time until the first sentence is ready to speak (what the caller actually waits for)
        "median_first_audio_ms": round(statistics.median(all_first)) if all_first else None,
        "p90_first_audio_ms": round(statistics.quantiles(all_first, n=10)[-1]) if len(all_first) >= 10 else None,
    }
    print("\n" + json.dumps(summary, indent=2))
    (HERE / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2,
                                                   ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
