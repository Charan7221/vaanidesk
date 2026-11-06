"""Pilot readiness: no double bookings, outage fallback, follow-up list, pilot confirmations, privacy, backups."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.business import Business
from app.db import Call, as_utc
from app.main import create_app
from app.pipeline.prompts import greeting_segments
from app.state import AppState
from app.tools.calendar import SlotUnavailable
from scripts.backup import counts, export, restore, verify

from .conftest import next_weekday
from .fakes import FakeLLM, FakeSTT, FakeTTS, text, tool
from .test_session import make_session, say, wait_for

AUTH = ("admin", "secret")


# ------------------------------------------------------------------ booking safety
async def test_simultaneous_calls_cannot_double_book(calendar, monday, at):
    results = await asyncio.gather(*[
        calendar.book(patient_name=f"P{i}", start=at(monday, "10:00"), provider_id="dr-rao") for i in range(5)
    ], return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, SlotUnavailable) for r in results if isinstance(r, Exception))
    # a cancelled slot can be booked again
    winner = next(r for r in results if not isinstance(r, Exception))
    await calendar.cancel(winner.id)
    assert await calendar.book(patient_name="Again", start=at(monday, "10:00"), provider_id="dr-rao")


# ------------------------------------------------------------------ outage fallback + follow-ups
def _boom(_messages):
    raise ConnectionError("LLM provider is down")


async def test_ai_outage_apologises_and_creates_follow_up(db, business, executor):
    session, transport, stt = await make_session(db, business, executor, FakeLLM([_boom]))
    await wait_for(lambda: session._response.done())
    await say(stt, "I need an appointment tomorrow")
    await wait_for(lambda: session.closed.is_set(), timeout=5)
    final = [e["text"] for e in transport.of_type("assistant") if e.get("final")]
    assert final[-1] == "Sorry, I'm having a technical problem. Our staff will call you back shortly."
    assert transport.hung_up and session.ctx.outcome == "callback"
    items = await db.follow_ups()
    assert [i.kind for i in items] == ["outage"] and "appointment tomorrow" in items[0].reason
    assert items[0].phone == "+919999999999"


async def test_call_that_ends_mid_booking_is_followed_up(db, business, executor, monday):
    llm = FakeLLM([tool("check_availability", "c1", date=monday.isoformat(), provider_id="dr-rao"),
                   text("Dr. Rao is free at nine. Shall I book it?")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "Appointment with Dr. Rao on Monday")
    await wait_for(lambda: any("Shall I" in e.get("text", "") for e in transport.of_type("assistant")))
    await session.close()  # caller hung up before booking
    items = await db.follow_ups()
    assert [i.kind for i in items] == ["abandoned"] and "Dr. Rao on Monday" in items[0].reason


async def test_finished_or_small_talk_calls_are_not_followed_up(db, business, executor):
    session, _, stt = await make_session(db, business, executor, FakeLLM([text("We open at nine.")]))
    await wait_for(lambda: session._response.done())
    await say(stt, "When do you open?")
    await wait_for(lambda: len(session.messages) >= 4)
    await session.close()
    assert await db.follow_ups() == []


async def test_request_callback_tool(executor, ctx, db):
    bad = await executor.run(ctx, "request_callback", {"reason": "billing question", "phone": "123"})
    assert bad["requested"] is False
    ok = await executor.run(ctx, "request_callback", {"reason": "billing question", "patient_name": "Ravi"})
    assert ok["requested"] is True and ctx.outcome == "callback"
    again = await executor.run(ctx, "request_callback", {"reason": "billing question"})
    assert again["requested"] is True
    items = await db.follow_ups()
    assert len(items) == 1 and items[0].kind == "callback" and items[0].patient_name == "Ravi"  # no duplicates


# ------------------------------------------------------------------ pilot mode
async def test_pilot_mode_bookings_wait_for_staff(executor, ctx, business, monday, monkeypatch):
    monkeypatch.setattr(business, "pilot_mode", True)
    res = await executor.run(ctx, "book_appointment", {"patient_name": "Sita", "date": monday.isoformat(),
                                                       "time": "09:00", "provider_id": "dr-rao"})
    assert res["booked"] is True and res["pending_confirmation"] is True
    appt = await executor.cal.db.get_appointment(res["appointment_id"])
    assert appt.confirmed is False
    # the slot is held while pending
    assert datetime.combine(monday, datetime.min.time().replace(hour=9), business.tz) not in \
        await executor.cal.available_slots(monday, "dr-rao")


def test_dashboard_attention_confirm_and_reject(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    state.business.pilot_mode = True
    day = next_weekday(state.business, 0)
    original = Business.now
    Business.now = lambda self: datetime(day.year, day.month, day.day, 8, 0, tzinfo=self.tz)
    try:
        with TestClient(create_app(settings, state)) as c:
            from app.tools.registry import ToolContext
            ctx = ToolContext(call_id="c1", channel="phone", caller_phone="+919346968532")
            a = c.portal.call(state.tools.run, ctx, "book_appointment", {
                "patient_name": "Sita", "date": day.isoformat(), "time": "09:00", "provider_id": "dr-rao"})
            t = c.portal.call(state.tools.run, ctx, "get_opd_token", {"provider_id": "dr-lakshmi",
                                                                      "patient_name": "Aarav"})
            assert t["pending_confirmation"] is True
            c.portal.call(state.tools.run, ctx, "request_callback", {"reason": "wants a home visit"})

            att = c.get("/api/attention", auth=AUTH).json()
            assert att["pilot_mode"] is True
            assert {p["kind"] for p in att["pending"]} == {"appointment", "token"}
            assert att["follow_ups"][0]["reason"] == "wants a home visit"

            assert c.post(f"/api/pending/appointment/{a['appointment_id']}", auth=AUTH,
                          json={"approve": True}).json() == {"approved": True}
            assert c.post(f"/api/pending/token/{t['token_id']}", auth=AUTH, json={"approve": False}).status_code == 200
            assert c.post("/api/pending/nope/x", auth=AUTH, json={"approve": True}).status_code == 422
            fu = att["follow_ups"][0]["id"]
            assert c.post(f"/api/follow-ups/{fu}/done", auth=AUTH, json={"note": "called back"}).json()["done"]

            after = c.get("/api/attention", auth=AUTH).json()
            assert after["pending"] == [] and after["follow_ups"] == []
            appt = c.portal.call(state.db.get_appointment, a["appointment_id"])
            token = c.portal.call(state.tools.opd.get, t["token_id"])
            assert appt.confirmed is True and token.status == "cancelled"
    finally:
        Business.now = original


# ------------------------------------------------------------------ privacy
def test_greeting_says_it_is_an_ai(business):
    first = greeting_segments(business, "en-IN")[0][1]
    assert "I'm an AI assistant" in first and first.endswith("How can I help you today?")
    off = greeting_segments(business.model_copy(update={"consent_notice": False}), "en-IN")[0][1]
    assert "AI assistant" not in off


async def test_old_transcripts_are_deleted(db):
    old = await db.create_call(channel="phone", caller="+919000000001")
    await db.add_turn(old.id, "user", "my private problem")
    await db.update_call(old.id, summary="private summary")
    new = await db.create_call(channel="phone", caller="+919000000002")
    await db.add_turn(new.id, "user", "recent")
    async with db.session() as s:
        await s.execute(update(Call).where(Call.id == old.id).values(started_at=datetime.now() - timedelta(days=100)))
        await s.commit()
    assert await db.purge_transcripts(90) == 1
    assert await db.purge_transcripts(0) == 0  # 0 = keep forever
    async with db.session() as s:
        assert (await s.get(Call, old.id)).summary is None
    assert (await counts(db))["turns"] == 1


# ------------------------------------------------------------------ backups
async def test_backup_export_restore_and_drill(db, calendar, monday, at, tmp_path):
    await calendar.book(patient_name="Ravi", start=at(monday, "09:00"), provider_id="dr-rao", phone="+919000000001")
    await db.add_follow_up("callback", "call me", phone="+919000000001")
    path = tmp_path / "backup.json.gz"
    exported = await export(db, path)
    assert exported["appointments"] == 1 and exported["follow_ups"] == 1
    assert (await verify(path))["appointments"] == 1

    from app.db import Database
    fresh = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await restore(fresh, path)
    restored = (await fresh.find_appointments(phone="+919000000001"))[0]
    assert restored.patient_name == "Ravi" and as_utc(restored.start) == as_utc(at(monday, "09:00"))
    try:
        await restore(fresh, path)  # refuses to restore over existing data
        raise AssertionError("should refuse")
    except SystemExit:
        pass
    await fresh.dispose()


# ------------------------------------------------------------------ cancellation safety
async def test_call_writes_finish_even_if_the_caller_is_cancelled(db):
    """A barge-in or a hang-up cancels the task that is writing; the write must still complete
    (a write cut off half-way leaves a broken SQLite connection that blocks every later write)."""
    call = await db.create_call(channel="phone", caller="+919000000001")
    task = asyncio.create_task(db.add_turn(call.id, "user", "hello"))
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    from app.background import drain
    await drain()
    async with db.session() as s:
        from sqlalchemy import select

        from app.db import Turn
        assert [t.text for t in (await s.execute(select(Turn))).scalars()] == ["hello"]
    await db.add_turn(call.id, "assistant", "still works")  # the database is not left locked
