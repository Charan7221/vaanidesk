"""Google Calendar sync logic, with the Google API mocked."""
from __future__ import annotations

from types import SimpleNamespace

from app.tools.calendar import GoogleCalendarSync, LocalCalendar


class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def make_sync(db, business, busy=None):
    cal = GoogleCalendarSync.__new__(GoogleCalendarSync)
    LocalCalendar.__init__(cal, db, business)
    cal.calendar_id = "clinic@group.calendar.google.com"
    cal._cal_path = "https://x/calendars/clinic"
    cal._creds = SimpleNamespace(service_account_email="bot@proj.iam.gserviceaccount.com")
    cal.requests = []

    async def fake_request(method, url, **kwargs):
        cal.requests.append((method, url, kwargs.get("json")))
        if url.endswith("/freeBusy"):
            return FakeResp({"calendars": {cal.calendar_id: {"busy": busy or []}}})
        if method == "POST":
            return FakeResp({"id": "evt123"})
        return FakeResp({})

    cal._request = fake_request
    return cal


async def test_google_busy_time_blocks_slots(db, business, monday, at):
    busy = [{"start": at(monday, "17:00").isoformat(), "end": at(monday, "18:00").isoformat()}]  # doctor's meeting
    cal = make_sync(db, business, busy)
    slots = await cal.available_slots(monday, "dr-rao")
    assert at(monday, "17:00") not in slots and at(monday, "17:40") not in slots
    assert at(monday, "18:00") in slots


async def test_booking_creates_and_cancel_deletes_google_event(db, business, monday, at):
    cal = make_sync(db, business)
    appt = await cal.book(patient_name="Sai", start=at(monday, "18:00"), provider_id="dr-rao", phone="+91999")
    inserts = [r for r in cal.requests if r[0] == "POST" and r[1].endswith("/events")]
    assert inserts and "Sai" in inserts[0][2]["summary"]
    stored = (await db.find_appointments(name="Sai"))[0]
    assert stored.external_event_id == "evt123"

    await cal.cancel(appt.id)
    assert ("DELETE", "https://x/calendars/clinic/events/evt123", None) in cal.requests
