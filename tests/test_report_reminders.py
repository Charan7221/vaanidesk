"""Results report for clinic owners, and day-before reminders."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import update

from app.business import Business
from app.db import Call
from app.main import create_app
from app.state import AppState
from app.tools.opd import OpdDesk
from app.tools.reminders import send_reminders

from .fakes import FakeLLM, FakeSTT, FakeTTS

AUTH = ("admin", "secret")


class FakeMessenger:
    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def notify_patient(self, biz, *, kind, msg, phone, email):
        self.sent.append((phone, msg.text))
        return {"sent": ["whatsapp"], "failed": []}


def local(business, day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=business.tz)


async def test_day_before_reminders(db, business, calendar, monday, monkeypatch):
    tuesday = monday + timedelta(days=1)
    now = {"t": local(business, monday, "08:00")}
    monkeypatch.setattr(Business, "now", lambda self: now["t"])
    call = await db.create_call(channel="phone", caller="+919000000001")
    await db.update_call(call.id, language="te-IN")
    await calendar.book(patient_name="Sai", start=local(business, tuesday, "10:00"), provider_id="dr-rao",
                        phone="+919000000001", call_id=call.id)
    await calendar.book(patient_name="Pending", start=local(business, tuesday, "10:20"), provider_id="dr-rao",
                        phone="+919000000002", confirmed=False)  # pilot mode, not confirmed: no reminder
    cancelled = await calendar.book(patient_name="Gone", start=local(business, tuesday, "10:40"),
                                    provider_id="dr-rao", phone="+919000000003")
    await calendar.cancel(cancelled.id)
    await calendar.book(patient_name="Later", start=local(business, tuesday + timedelta(days=1), "10:00"),
                        provider_id="dr-rao", phone="+919000000004")  # the day after tomorrow: not yet
    await calendar.book(patient_name="NoPhone", start=local(business, tuesday, "11:00"), provider_id="dr-rao")
    await OpdDesk(db, business).issue(provider_id="dr-lakshmi", day=tuesday, patient_name="Aarav",
                                      phone="+919000000005", session_hint="evening")

    msg = FakeMessenger()
    now["t"] = local(business, monday, "17:00")
    assert (await send_reminders(db, business, msg))["sent"] == 0  # before 18:00
    now["t"] = local(business, monday, "18:05")
    out = await send_reminders(db, business, msg)
    assert out == {"sent": 2, "failed": 0, "skipped_no_phone": 1}
    texts = dict(msg.sent)
    assert "రేపు" in texts["+919000000001"] and "Dr. Srinivasa Rao" in texts["+919000000001"]  # Telugu caller
    assert "OPD token 1" in texts["+919000000005"] and "evening OPD from 5:00 PM" in texts["+919000000005"]
    assert (await send_reminders(db, business, msg))["sent"] == 0  # never twice

    class Off:
        enabled = False
    assert (await send_reminders(db, business, Off(), force=True))["not_configured"] is True


def test_results_report(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    with TestClient(create_app(settings, state)) as c:
        db, biz = state.db, state.business
        now = datetime.now(UTC)
        # a weekday, 3 days ago: two overlapping calls at 10:00 local, one at 22:00 (clinic closed)
        day = (now - timedelta(days=3)).astimezone(biz.tz).date()
        while not biz.opening_windows(day):
            day -= timedelta(days=1)

        def add(start_local, minutes, outcome, lang="te-IN"):
            call = c.portal.call(db.create_call, "phone", "+919000000001")

            async def fix():
                async with db.session() as s:
                    await s.execute(update(Call).where(Call.id == call.id).values(
                        started_at=start_local.astimezone(UTC),  # the app always stores UTC
                        ended_at=(start_local + timedelta(minutes=minutes)).astimezone(UTC), status="completed",
                        outcome=outcome, language=lang))
                    await s.commit()
            c.portal.call(fix)
            return call.id

        booked_call = add(local(biz, day, "10:00"), 3, "booked")
        add(local(biz, day, "10:01"), 2, "transferred", "hi-IN")  # while the first call was still on
        add(local(biz, day, "22:00"), 2, "info")  # after hours
        future = day + timedelta(days=7)  # same weekday next week: open
        c.portal.call(lambda: state.calendar.book(patient_name="Sai", start=local(biz, future, "10:00"),
                                                  provider_id="dr-rao", phone="+919000000001", call_id=booked_call))

        async def fee():
            async with db.session() as s:
                from app.db import Appointment
                await s.execute(update(Appointment).values(fee_type="new", fee_amount=400))
                await s.commit()
        c.portal.call(fee)

        r = c.get("/api/report?days=7", auth=AUTH).json()
        assert r["calls_answered"] == 3
        assert r["after_hours_calls"] == 1 and r["calls_while_another_call_was_on"] == 1
        assert r["would_have_missed"] == 2
        assert r["handled_without_staff"] == 2 and r["transferred_to_staff"] == 1
        assert r["staff_minutes_saved"] == 5
        assert r["appointments_booked_by_ai"] == 1 and r["consultation_fees_from_ai_bookings"] == 400
        assert r["languages"] == {"te-IN": 2, "hi-IN": 1}
        assert len(r["calls_by_day"]) == 7 and sum(x["calls"] for x in r["calls_by_hour"]) == 3
        assert c.get("/report", auth=AUTH).status_code == 200
        assert c.post("/api/reminders/send-now", auth=AUTH).json()["not_configured"] is True
