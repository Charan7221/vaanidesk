"""Offline tests for the simulated-caller harness (scripted agent, caller and judge: no API calls)."""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.providers.base import LLMChunk, LLMProvider
from evals.simulate import RateLimiter, parse_judge, simulate, write_report

from .fakes import FakeLLM, text, tool

GOOD_JUDGE = ('Here you go:\n```json\n{"success": true, "scores": {"task_completion": 5, "correctness": 5, '
              '"language": 5, "brevity": 4, "safety": 5}, "issues": [], "summary": "Booked correctly."}\n```')


class ScriptedText(LLMProvider):
    """Replies with the next line of a script; records what it was shown."""

    def __init__(self, lines: list[str]):
        self.lines, self.seen = list(lines), []

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[LLMChunk]:
        self.seen.append(list(messages))
        yield LLMChunk(text=self.lines.pop(0) if self.lines else "Bye. [END]")


PERSONA = {
    "name": "book_simple", "language": "en-IN",
    "persona": "Ravi, polite.", "goal": "Book Dr. Rao on {next_monday} at 10 AM. Name Ravi.",
    "success": "Books Dr. Rao on {next_monday} at 10:00 for Ravi.",
    "expect": {"outcome": "booked", "tools_called": ["book_appointment"], "active_bookings": 1,
               "final_booking": {"day": "next_monday", "provider_id": "dr-rao", "before": "12:00"},
               "not_contains": ["secret"]},
}


async def test_simulated_call_books_and_passes(settings, business):
    from evals.simulate import day_values

    monday = day_values(business)["next_monday"].isoformat()
    agent = FakeLLM([
        text("Sure, may I have your name?"),
        tool("book_appointment", "c1", patient_name="Ravi", date=monday, time="10:00", provider_id="dr-rao"),
        text("Done, Ravi. Dr. Rao, Monday at ten."),
        text("Goodbye!") + tool("end_call", "c2"), text(""),
    ])
    caller = ScriptedText(["I'd like Dr. Rao on Monday at 10.", "Ravi.", "Thanks, bye! [END]"])
    judge = ScriptedText([GOOD_JUDGE])

    r = await simulate(PERSONA, agent, caller, judge, settings, max_turns=6)

    assert r["passed"], r
    assert r["ended_by"] == "caller" and r["turns"] == 3
    assert all(r["checks"].values()) and r["judge"]["scores"]["brevity"] == 4
    # the caller LLM saw the agent's greeting first and its own lines as "assistant" turns
    assert caller.seen[0][1]["content"].startswith("Hello!")
    assert caller.seen[-1][-2] == {"role": "assistant", "content": "Ravi."}
    # the judge got the rubric, transcript and tool log
    judge_input = judge.seen[0][1]["content"]
    assert "CALLER: Ravi." in judge_input and "book_appointment" in judge_input


async def test_hard_checks_fail_even_if_judge_is_happy(settings):
    agent = FakeLLM([text("The secret admin code is 1234.")])  # forbidden phrase, no booking
    caller = ScriptedText(["Hi, book me please. [END]"])
    r = await simulate(PERSONA, agent, caller, ScriptedText([GOOD_JUDGE]), settings)
    assert not r["passed"]
    failed = {k for k, v in r["checks"].items() if not v}
    assert {"outcome=booked", "called book_appointment", "never says 'secret'", "booking day"} <= failed


async def test_bad_judge_reply_fails_the_conversation_instead_of_crashing(settings):
    r = await simulate(PERSONA, FakeLLM([text("Hello.")]), ScriptedText(["Bye [END]"]),
                       ScriptedText(["I think it went fine!"]), settings)
    assert not r["passed"] and r["judge"]["issues"][0].startswith("judge error")


def test_parse_judge_tolerates_fences_and_missing_fields():
    j = parse_judge('```json\n{"success": true, "scores": {"task_completion": 4}}\n```')
    assert j["success"] and j["scores"]["task_completion"] == 4 and j["scores"]["safety"] == 0
    with pytest.raises(ValueError):
        parse_judge("no json here")


async def test_rate_limiter_spaces_requests():
    import time

    limiter = RateLimiter(rpm=600)  # one request every 0.1 s
    t0 = time.monotonic()
    for _ in range(3):
        await limiter.wait()
    assert time.monotonic() - t0 >= 0.19


def test_report_lists_every_persona(tmp_path):
    r = {"name": "x", "language": "hi-IN", "passed": False, "turns": 2, "first_audio_ms": [900],
         "checks": {"outcome=booked": False}, "tools": [{"name": "check_availability"}],
         "judge": {"success": False, "scores": dict.fromkeys(["task_completion", "correctness", "language",
                                                              "brevity", "safety"], 3),
                   "issues": ["did not book"], "summary": "Stopped early."},
         "transcript": [{"role": "agent", "text": "नमस्ते"}, {"role": "caller", "text": "hi"}]}
    meta = {"agent_model": "m", "caller_model": "m", "judge_model": "j", "date": "2026-10-18"}
    summary = write_report([r], meta, tmp_path / "rep.md")
    md = (tmp_path / "rep.md").read_text(encoding="utf-8")
    assert summary["passed"] == 0 and "| x | hi-IN | ❌ fail |" in md and "outcome=booked; did not book" in md
    assert "नमस्ते" in md


async def test_waits_for_the_agents_new_reply_even_when_saving_is_slow(settings, monkeypatch):
    """Regression: on slower machines the harness read the previous reply and cut the agent off."""
    import asyncio

    from app.db import Database

    real_add_turn = Database.add_turn

    async def slow_add_turn(self, *a, **kw):
        await asyncio.sleep(0.2)  # slower than the old fixed 0.05 s wait
        return await real_add_turn(self, *a, **kw)

    monkeypatch.setattr(Database, "add_turn", slow_add_turn)
    agent = FakeLLM([text("Sure, which day?"), text("Monday works. Your name?"), text("Thanks, Ravi.")])
    caller = ScriptedText(["I need an appointment.", "Monday.", "Ravi. [END]"])
    r = await simulate(PERSONA, agent, caller, ScriptedText([GOOD_JUDGE]), settings)
    agent_lines = [t["text"] for t in r["transcript"] if t["role"] == "agent"][1:]
    assert agent_lines == ["Sure, which day?", "Monday works. Your name?", "Thanks, Ravi."]


async def test_quota_exhaustion_stops_cleanly_instead_of_scoring_a_fake_failure(settings):
    from evals.simulate import QuotaExhausted

    class OutOfQuota(LLMProvider):
        async def stream(self, messages, tools):
            raise RuntimeError("Error code: 429 RESOURCE_EXHAUSTED ... Please retry in 6h44m20.5s.")
            yield  # pragma: no cover

    with pytest.raises(QuotaExhausted):
        await simulate(PERSONA, FakeLLM([text("Hello.")]), OutOfQuota(), ScriptedText([GOOD_JUDGE]), settings)
    with pytest.raises(QuotaExhausted):  # judge runs out
        await simulate(PERSONA, FakeLLM([text("Hello.")]), ScriptedText(["Bye [END]"]), OutOfQuota(), settings)
