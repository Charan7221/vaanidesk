"""Dashboard calendar: blocked time, staff bookings, week view and the .ics subscription feed."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.calendar import _fold, feed_token
from app.main import create_app
from app.state import AppState

from .conftest import next_weekday
from .fakes import FakeLLM, FakeSTT, FakeTTS

AUTH = ("admin", "secret")


def make_client(settings):
    app = create_app(settings, AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS()))
    return TestClient(app)


async def test_block_removes_slots_for_ai(executor, ctx, db, monday, at):
    await db.add_block(at(monday, "17:00"), at(monday, "18:00"), "dr-rao", "Meeting")
    res = await executor.run(ctx, "check_availability", {"date": monday.isoformat(), "part_of_day": "evening",
                                                         "provider_id": "dr-rao"})
    assert res["available"][0] == "18:00"
    # clinic-wide block affects every doctor
    await db.add_block(at(monday, "09:00"), at(monday, "13:00"), None, "Holiday")
    res = await executor.run(ctx, "check_availability", {"date": monday.isoformat(), "provider_id": "dr-lakshmi",
                                                         "part_of_day": "morning"})
    assert res["available"] == []


def test_calendar_api_flow(settings, business):
    monday = next_weekday(business, 0).isoformat()
    with make_client(settings) as c:
        assert c.get("/api/calendar").status_code == 401
        assert c.get("/calendar", auth=AUTH).status_code == 200

        r = c.post("/api/blocks", auth=AUTH, json={"date": monday, "start": "17:00", "end": "18:00",
                                                    "provider_id": "dr-rao", "reason": "Meeting"})
        assert r.status_code == 200
        block_id = r.json()["id"]
        assert c.post("/api/blocks", auth=AUTH, json={"date": monday, "start": "18:00", "end": "17:00"}).status_code == 422

        # staff can't book into a blocked slot, but can book a free one
        bad = c.post("/api/appointments", auth=AUTH, json={"date": monday, "time": "17:20", "provider_id": "dr-rao",
                                                            "patient_name": "X"})
        assert bad.status_code == 409
        ok = c.post("/api/appointments", auth=AUTH, json={"date": monday, "time": "18:00", "provider_id": "dr-rao",
                                                           "patient_name": "Walk-in Ravi", "phone": "+919000000001"})
        assert ok.status_code == 200

        week = c.get(f"/api/calendar?start={monday}&provider_id=dr-rao", auth=AUTH).json()
        assert week["days"][0]["open"] == [["09:00", "13:00"], ["17:00", "20:00"]]
        assert week["blocks"][0]["reason"] == "Meeting" and week["blocks"][0]["start"].endswith("17:00+05:30")
        assert week["appointments"][0]["patient_name"] == "Walk-in Ravi"
        assert week["appointments"][0]["booked_by"] == "Staff"

        assert c.delete(f"/api/blocks/{block_id}", auth=AUTH).json() == {"deleted": True}
        assert c.delete(f"/api/blocks/{block_id}", auth=AUTH).status_code == 404


def test_ics_feed(settings, business):
    monday = next_weekday(business, 0).isoformat()
    with make_client(settings) as c:
        c.post("/api/appointments", auth=AUTH, json={"date": monday, "time": "09:20", "provider_id": "dr-rao",
                                                      "patient_name": "Anjali, Sharma", "reason": "fever; cough"})
        info = c.get("/api/calendar-feed", auth=AUTH).json()
        assert info["url"].endswith(f"/calendar/{feed_token(settings)}.ics") and info["public"] is False

        assert c.get("/calendar/wrong-token.ics").status_code == 404
        r = c.get(f"/calendar/{feed_token(settings)}.ics")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
        body = r.text
        assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
        assert "SUMMARY:Anjali\\, Sharma — Dr. Srinivasa Rao" in body
        assert "fever\\; cough" in body.replace("\r\n ", "")
        assert "STATUS:CONFIRMED" in body
        assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_fold_long_lines():
    line = "DESCRIPTION:" + "అ" * 60
    folded = _fold(line)
    assert all(len(p.encode()) <= 75 for p in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line
