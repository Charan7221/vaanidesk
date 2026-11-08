"""OPD walk-in tokens: numbering, live queue and waiting time, guardrails, agent tools and the reception API."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.business import Business
from app.main import create_app
from app.pipeline.prompts import system_prompt
from app.state import AppState
from app.tools.opd import OpdDesk, OpdError
from app.tools.registry import ToolContext

from .conftest import next_weekday
from .fakes import FakeLLM, FakeSTT, FakeTTS

AUTH = ("admin", "secret")


@pytest.fixture
def opd(db, business) -> OpdDesk:
    return OpdDesk(db, business)


@pytest.fixture
def clock(business, monday, monkeypatch):
    """Freeze the clinic clock (default: Monday 08:00, before the morning OPD) and let tests move it."""
    state = {"now": datetime(monday.year, monday.month, monday.day, 8, 0, tzinfo=business.tz)}
    monkeypatch.setattr(Business, "now", lambda self: state["now"])

    def set_time(hhmm: str, day=None) -> datetime:
        d = day or monday
        h, m = map(int, hhmm.split(":"))
        state["now"] = datetime(d.year, d.month, d.day, h, m, tzinfo=business.tz)
        return state["now"]

    return set_time


async def issue(opd, monday, name, provider="dr-rao", phone="+919000000001", **kw):
    token, created = await opd.issue(provider_id=provider, day=monday, patient_name=name, phone=phone, **kw)
    return token, created


# ------------------------------------------------------------------ desk logic
async def test_tokens_are_numbered_per_doctor_and_session(opd, monday, clock):
    a, _ = await issue(opd, monday, "Ravi")
    b, _ = await issue(opd, monday, "Sita", phone="+919000000002")
    c, _ = await issue(opd, monday, "Aarav", provider="dr-lakshmi")
    d, _ = await issue(opd, monday, "Ravi", session_hint="evening")
    assert (a.number, b.number, c.number, d.number) == (1, 2, 1, 1)
    assert a.session == "09:00" and d.session == "17:00"

    again, created = await issue(opd, monday, "ravi")  # same patient, same session: same token
    assert created is False and again.id == a.id


async def test_waiting_time_and_live_queue(opd, monday, clock):
    t1, _ = await issue(opd, monday, "A", phone="+919000000001")
    t2, _ = await issue(opd, monday, "B", phone="+919000000002")
    t3, _ = await issue(opd, monday, "C", phone="+919000000003")
    pos = await opd.position(t3)
    assert pos["patients_ahead"] == 2 and pos["now_serving"] is None
    assert pos["estimated_turn"] == "9:20 AM" and pos["please_arrive_by"] == "9:05 AM"

    clock("09:00")
    assert (await opd.call_next(monday, "dr-rao", "09:00"))["now_serving"] == 1
    pos = await opd.position(t3)
    assert pos["now_serving"] == 1 and pos["patients_ahead"] == 1 and pos["estimated_turn"] == "9:20 AM"

    clock("09:12")
    nxt = await opd.call_next(monday, "dr-rao", "09:00")
    assert nxt == {"finished": 1, "now_serving": 2, "patient_name": "B", "waiting": 1}
    pos = await opd.position(t3)
    assert pos["patients_ahead"] == 0 and pos["estimated_turn"] == "9:22 AM" and pos["please_arrive_by"] == "now"

    # the doctor is running 30 minutes late: everyone's estimate moves
    await opd.set_session_status(monday, "dr-rao", "17:00", delay_minutes=30)
    e1, _ = await issue(opd, monday, "Late", session_hint="evening")
    assert (await opd.position(e1))["estimated_turn"] == "5:30 PM"
    assert (await opd.position(e1))["doctor_running_late_minutes"] == 30


async def test_phone_tokens_stop_before_session_end_but_desk_can_still_issue(opd, monday, clock):
    clock("12:40")  # morning OPD ends 13:00; phone tokens stop at 12:30
    with pytest.raises(OpdError) as exc:
        await issue(opd, monday, "Ravi", session_hint="morning")
    assert "closed" in exc.value.reason
    assert exc.value.extra["next_session_today"] == "evening from 5:00 PM"

    token, _ = await issue(opd, monday, "Ravi")  # no session named: the next one still giving tokens
    assert token.session == "17:00"
    desk, _ = await issue(opd, monday, "Walk-in Sita", session_hint="morning", source="staff")
    assert desk.session == "09:00"

    clock("13:05")
    with pytest.raises(OpdError):  # after the session has ended, not even the desk
        await issue(opd, monday, "Too late", session_hint="morning", source="staff")


async def test_guardrails(opd, monday, clock, business):
    with pytest.raises(OpdError, match="only given for today"):
        await issue(opd, monday + timedelta(days=business.opd.advance_days + 1), "Ravi")
    with pytest.raises(OpdError, match="past"):
        await issue(opd, monday - timedelta(days=1), "Ravi")
    saturday = monday + timedelta(days=5)
    clock("08:00", saturday)
    with pytest.raises(OpdError, match="closed on Sunday"):
        await issue(opd, saturday + timedelta(days=1), "Ravi")
    with pytest.raises(OpdError, match="no night OPD"):
        await issue(opd, saturday, "Ravi", session_hint="night")  # Saturday: morning only
    clock("08:00")

    await opd.set_session_status(monday, "dr-lakshmi", "09:00", cancelled=True, note="Doctor on leave")
    with pytest.raises(OpdError) as exc:
        await issue(opd, monday, "Aarav", provider="dr-lakshmi")
    assert "cancelled" in exc.value.reason and exc.value.extra["note"] == "Doctor on leave"

    business.opd.max_tokens_per_session = 2
    await issue(opd, monday, "A", phone="+919000000001")
    await issue(opd, monday, "B", phone="+919000000002")
    with pytest.raises(OpdError, match="full"):
        await issue(opd, monday, "C", phone="+919000000003")


async def test_patient_registration_reuses_number(opd):
    a = await opd.register_patient("Ravi Kumar", "+919000000001", age=40)
    b = await opd.register_patient("ravi kumar", "+919000000001")
    child = await opd.register_patient("Aarav", "+919000000001", age=6)  # family member, same phone
    assert a.number == b.number == 1001 and child.number == 1002


# ------------------------------------------------------------------ agent tools
async def test_get_token_tool_and_status(executor, ctx, monday, clock):
    no_name = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Unknown"})
    assert no_name["issued"] is False and "name" in no_name["reason"]
    no_doc = await executor.run(ctx, "get_opd_token", {"patient_name": "Ravi"})
    assert no_doc["issued"] is False and len(no_doc["doctors"]) == 2

    res = await executor.run(ctx, "get_opd_token", {"provider_id": "Dr. Rao", "patient_name": "Ravi Kumar",
                                                    "age": 42, "reason": "fever"})
    assert res["issued"] is True and res["token_number"] == 1 and res["patient_number"] == 1001
    assert res["doctor"] == "Dr. Srinivasa Rao" and res["session"].startswith("morning")
    assert ctx.outcome == "token" and ctx.opd_token_ids == [res["token_id"]]
    again = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Ravi Kumar"})
    assert again["already_had_token"] is True and again["token_number"] == 1

    status = await executor.run(ctx, "check_opd_status", {})
    assert [t["token_number"] for t in status["your_tokens"]] == [1]
    rao = next(d for d in status["doctors"] if d["provider_id"] == "dr-rao")
    assert rao["sessions"][0]["waiting"] == 1 and rao["sessions"][0]["accepting_tokens"] is True
    assert "Ravi" not in str(rao)  # other patients' names are never given to callers

    bad_phone = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Sita",
                                                          "phone": "12345"})
    assert bad_phone["issued"] is False and "invalid phone" in bad_phone["reason"]


async def test_token_ownership(executor, ctx, monday, clock):
    res = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Ravi Kumar",
                                                    "phone": "93469 68532"})
    stranger = ToolContext(call_id="other", channel="phone", caller_phone="+919876543210")
    assert (await executor.run(stranger, "check_opd_status", {}))["your_tokens"] == []
    denied = await executor.run(stranger, "cancel_opd_token", {"token_id": res["token_id"]})
    assert denied["cancelled"] is False and denied["reason"] == "not the caller's token"

    # the stranger says the right number: now they can see (and cancel) it
    found = await executor.run(stranger, "check_opd_status", {"phone": "9346968532"})
    assert [t["token_number"] for t in found["your_tokens"]] == [1]
    ok = await executor.run(stranger, "cancel_opd_token", {"token_id": res["token_id"]})
    assert ok == {"cancelled": True, "token_number": 1}
    assert (await executor.run(stranger, "check_opd_status", {}))["your_tokens"] == []


async def test_opd_disabled(executor, ctx, business, monday, clock):
    business.opd.enabled = False
    res = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Ravi"})
    assert res["issued"] is False and "appointment" in res["instruction"]
    assert "OPD tokens" not in system_prompt(business)
    business.opd.enabled = True
    assert "get_opd_token" in system_prompt(business)


# ------------------------------------------------------------------ reception API
def test_reception_api_flow(settings):
    app_state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    day = next_weekday(app_state.business, 0)
    fixed = datetime(day.year, day.month, day.day, 8, 30, tzinfo=app_state.business.tz)
    original = Business.now
    Business.now = lambda self: fixed
    try:
        with TestClient(create_app(settings, app_state)) as c:
            assert c.get("/opd").status_code == 401
            assert c.get("/opd", auth=AUTH).status_code == 200

            r = c.post("/api/opd/tokens", auth=AUTH, json={"provider_id": "dr-rao", "patient_name": "Walk-in Ravi",
                                                            "session": "09:00", "phone": "9000000001", "age": 30})
            assert r.status_code == 200 and r.json()["number"] == 1
            c.post("/api/opd/tokens", auth=AUTH, json={"provider_id": "dr-rao", "patient_name": "Sita",
                                                        "session": "09:00"})
            assert c.post("/api/opd/tokens", auth=AUTH, json={"provider_id": "nobody",
                                                               "patient_name": "X Y"}).status_code == 422
            assert c.post("/api/opd/tokens", auth=AUTH, json={"provider_id": "dr-rao", "patient_name": "Bad",
                                                               "phone": "123"}).status_code == 422

            nxt = c.post("/api/opd/next", auth=AUTH, json={"provider_id": "dr-rao", "session": "09:00"}).json()
            assert nxt["now_serving"] == 1 and nxt["waiting"] == 1

            board = c.get("/api/opd", auth=AUTH).json()
            rao = next(d for d in board["doctors"] if d["id"] == "dr-rao")
            morning = rao["sessions"][0]
            assert morning["now_serving"] == 1 and morning["waiting"] == 1
            assert [t["status"] for t in morning["tokens"]] == ["in_consultation", "waiting"]
            sita = morning["tokens"][1]["id"]

            assert c.post(f"/api/opd/tokens/{sita}/status", auth=AUTH,
                          json={"status": "skipped"}).json()["status"] == "skipped"
            assert c.post(f"/api/opd/tokens/{sita}/status", auth=AUTH, json={"status": "bogus"}).status_code == 422
            assert c.post("/api/opd/tokens/nope/status", auth=AUTH, json={"status": "done"}).status_code == 404

            r = c.post("/api/opd/session-status", auth=AUTH, json={"provider_id": "dr-lakshmi", "cancelled": True,
                                                                    "note": "On leave"})
            assert r.json() == {"updated": ["09:00", "17:00"]}
            lakshmi = next(d for d in c.get("/api/opd", auth=AUTH).json()["doctors"] if d["id"] == "dr-lakshmi")
            assert all(s["cancelled"] and not s["accepting_tokens"] for s in lakshmi["sessions"])
            assert c.post("/api/opd/tokens", auth=AUTH, json={"provider_id": "dr-lakshmi",
                                                               "patient_name": "Aarav"}).status_code == 409
    finally:
        Business.now = original


def test_appointments_show_in_their_opd_session(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    state.business.pilot_mode = True
    day = next_weekday(state.business, 0)
    original = Business.now
    Business.now = lambda self: datetime(day.year, day.month, day.day, 8, 0, tzinfo=self.tz)
    try:
        with TestClient(create_app(settings, state)) as c:
            ctx = ToolContext(call_id="c1", channel="phone", caller_phone="+919346968532")
            a = c.portal.call(state.tools.run, ctx, "book_appointment", {
                "patient_name": "Sreelakshmi", "date": day.isoformat(), "time": "17:40", "provider_id": "dr-lakshmi"})
            assert a["booked"] and a["pending_confirmation"]
            board = c.get(f"/api/opd?day={day.isoformat()}", auth=AUTH).json()
            lakshmi = next(d for d in board["doctors"] if d["id"] == "dr-lakshmi")
            by_session = {s["session"]: s["appointments"] for s in lakshmi["sessions"]}
            evening = next(v for k, v in by_session.items() if k >= "16:00")
            assert [x["patient_name"] for x in evening] == ["Sreelakshmi"] and evening[0]["time"] == "17:40"
            assert evening[0]["confirmed"] is False and evening[0]["visit"] is None
            assert all(not v for k, v in by_session.items() if k < "16:00")  # not in the morning session
            rao = next(d for d in board["doctors"] if d["id"] == "dr-rao")
            assert all(not s["appointments"] for s in rao["sessions"])  # only under her own doctor

            url = f"/api/opd/appointments/{a['appointment_id']}/visit"
            assert c.post(url, auth=AUTH, json={"visit": "teleported"}).status_code == 422
            assert c.post(url, auth=AUTH, json={"visit": "arrived"}).json()["visit"] == "arrived"
            assert c.post(url, auth=AUTH, json={"visit": "done"}).status_code == 200
            assert c.post("/api/opd/appointments/nope/visit", auth=AUTH, json={"visit": "done"}).status_code == 404
            board = c.get(f"/api/opd?day={day.isoformat()}", auth=AUTH).json()
            lakshmi = next(d for d in board["doctors"] if d["id"] == "dr-lakshmi")
            assert [x["visit"] for s in lakshmi["sessions"] for x in s["appointments"]] == ["done"]

            # rejected (cancelled) appointments leave the queue
            c.post(f"/api/pending/appointment/{a['appointment_id']}", auth=AUTH, json={"approve": False})
            board = c.get(f"/api/opd?day={day.isoformat()}", auth=AUTH).json()
            assert not any(s["appointments"] for d in board["doctors"] for s in d["sessions"])
    finally:
        Business.now = original
