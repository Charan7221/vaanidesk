"""Appointment calendar.

``LocalCalendar`` stores bookings in the app database and derives free slots from the
business opening hours. ``GoogleCalendarSync`` (optional) additionally mirrors bookings
to a Google Calendar and treats existing Google events as busy time.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import httpx

from ..business import Business
from ..db import Appointment, Database, as_utc

log = logging.getLogger(__name__)


class SlotUnavailable(Exception):
    pass


@dataclass
class BusyBlock:
    start: datetime
    end: datetime


class LocalCalendar:
    def __init__(self, db: Database, business: Business, min_notice_minutes: int = 15):
        self.db = db
        self.biz = business
        self.min_notice = timedelta(minutes=min_notice_minutes)

    async def external_busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        return []

    async def available_slots(self, day: date, provider_id: str | None = None,
                              now: datetime | None = None) -> list[datetime]:
        now = now or self.biz.now()
        if day < now.date() or day > now.date() + timedelta(days=self.biz.booking_horizon_days):
            return []
        candidates = self.biz.candidate_slots(day)
        if not candidates:
            return []
        step = timedelta(minutes=self.biz.slot_minutes)
        day_start, day_end = candidates[0], candidates[-1] + step
        booked = await self.db.appointments_between(day_start, day_end, provider_id)
        busy = [BusyBlock(as_utc(a.start), as_utc(a.end)) for a in booked]
        busy += [BusyBlock(as_utc(b.start), as_utc(b.end))
                 for b in await self.db.blocks_between(day_start, day_end, provider_id)]
        busy += await self.external_busy(day_start, day_end)
        free = []
        for slot in candidates:
            s_utc, e_utc = as_utc(slot), as_utc(slot + step)
            if slot < now + self.min_notice:
                continue
            if any(b.start < e_utc and b.end > s_utc for b in busy):
                continue
            free.append(slot)
        return free

    async def book(self, *, patient_name: str, start: datetime, provider_id: str | None,
                   phone: str | None = None, reason: str | None = None, call_id: str | None = None) -> Appointment:
        free = await self.available_slots(start.date(), provider_id)
        if start not in free:
            raise SlotUnavailable(start.isoformat())
        end = start + timedelta(minutes=self.biz.slot_minutes)
        appt = await self.db.add_appointment(
            patient_name=patient_name, phone=phone, provider_id=provider_id, start=as_utc(start),
            end=as_utc(end), reason=reason, call_id=call_id,
        )
        await self.after_booking(appt)
        return appt

    async def after_booking(self, appt: Appointment) -> None:
        return None

    async def find(self, phone: str | None = None, name: str | None = None) -> list[Appointment]:
        return await self.db.find_appointments(phone=phone, name=name, after=datetime.now(UTC))

    async def cancel(self, appointment_id: str) -> Appointment | None:
        return await self.db.cancel_appointment(appointment_id)


class GoogleCalendarSync(LocalCalendar):
    """Mirror bookings to a Google Calendar using a service account.

    Setup: share the calendar with the service account's e-mail ("Make changes to events").
    - Every booking is inserted as a Google Calendar event (and deleted again on cancel).
    - Any event already in the calendar (e.g. the doctor's meeting) blocks those slots.
    """

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, db: Database, business: Business, calendar_id: str,
                 service_account_file: str = "", service_account_info: dict | None = None):
        super().__init__(db, business)
        from google.oauth2 import service_account  # optional dependency

        self.calendar_id = calendar_id
        if service_account_info:
            self._creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=self.SCOPES)
        else:
            self._creds = service_account.Credentials.from_service_account_file(service_account_file,
                                                                                 scopes=self.SCOPES)
        self._cal_path = f"{self.API}/calendars/{quote(calendar_id, safe='')}"

    @property
    def service_account_email(self) -> str:
        return self._creds.service_account_email

    async def _token(self) -> str:
        from google.auth.transport.requests import Request

        if not self._creds.valid:
            await asyncio.to_thread(self._creds.refresh, Request())
        return self._creds.token

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(method, url, headers={"Authorization": f"Bearer {await self._token()}"}, **kwargs)
            if r.status_code >= 400:
                raise RuntimeError(f"Google Calendar {method} {r.status_code}: {r.text[:300]}")
            return r

    async def verify(self) -> str:
        """Raise with a helpful message if the calendar isn't reachable/writable. Returns its name."""
        r = await self._request("GET", self._cal_path)
        return r.json().get("summary", self.calendar_id)

    async def external_busy(self, start: datetime, end: datetime) -> list[BusyBlock]:
        try:
            r = await self._request("POST", f"{self.API}/freeBusy", json={
                "timeMin": as_utc(start).isoformat(), "timeMax": as_utc(end).isoformat(),
                "items": [{"id": self.calendar_id}]})
            cal = r.json()["calendars"].get(self.calendar_id, {})
            if cal.get("errors"):
                log.warning("Google freeBusy errors: %s (is the calendar shared with %s?)",
                            cal["errors"], self.service_account_email)
            return [BusyBlock(as_utc(datetime.fromisoformat(b["start"].replace("Z", "+00:00"))),
                              as_utc(datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
                    for b in cal.get("busy", [])]
        except Exception as exc:  # never block bookings on a calendar outage
            log.warning("Google freeBusy failed: %s", exc)
            return []

    async def after_booking(self, appt: Appointment) -> None:
        provider = self.biz.provider(appt.provider_id)
        body = {
            "summary": f"{appt.patient_name} — {provider.name if provider else 'Appointment'}",
            "description": (f"Phone: {appt.phone or '-'}\nReason: {appt.reason or '-'}\n"
                            f"Ref: {appt.id}\nBooked by VaaniDesk AI receptionist"),
            "start": {"dateTime": as_utc(appt.start).isoformat(), "timeZone": self.biz.timezone},
            "end": {"dateTime": as_utc(appt.end).isoformat(), "timeZone": self.biz.timezone},
        }
        try:
            r = await self._request("POST", f"{self._cal_path}/events", json=body)
            event_id = r.json().get("id")
            appt.external_event_id = event_id
            await self.db.set_external_event_id(appt.id, event_id)
        except Exception as exc:
            log.warning("Google event insert failed: %s", exc)

    async def cancel(self, appointment_id: str) -> Appointment | None:
        appt = await super().cancel(appointment_id)
        if appt and appt.external_event_id:
            try:
                await self._request("DELETE", f"{self._cal_path}/events/{quote(appt.external_event_id, safe='')}")
            except Exception as exc:
                log.warning("Google event delete failed: %s", exc)
        return appt
