"""Clinic calendar: week view data, blocked time, staff bookings and a subscribable .ics feed.

No Google setup needed:
  * Staff see and manage the schedule on the dashboard (/calendar).
  * The secret .ics link can be subscribed to from Google Calendar / iPhone / Outlook so
    bookings show up on their phones (read-only).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import Appointment, as_utc
from ..phone import validate_phone
from ..security import require_admin
from ..tools.calendar import SlotUnavailable

router = APIRouter(tags=["calendar"])
admin = [Depends(require_admin)]


# ------------------------------------------------------------------ helpers
def feed_token(settings) -> str:
    """Stable secret for the calendar feed URL (override with CALENDAR_FEED_TOKEN)."""
    if settings.calendar_feed_token:
        return settings.calendar_feed_token
    return hmac.new(settings.admin_password.encode(), b"vaanidesk-calendar-feed", hashlib.sha256).hexdigest()[:32]


def _local(dt: datetime, tz) -> str:
    return as_utc(dt).astimezone(tz).isoformat(timespec="minutes")


def _parse_hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value.strip()[:5])
    except ValueError as exc:
        raise HTTPException(422, f"invalid time {value!r}, expected HH:MM") from exc


# ------------------------------------------------------------------ week view
@router.get("/api/calendar", dependencies=admin)
async def week(request: Request, start: date | None = None, days: int = 7, provider_id: str | None = None) -> dict:
    state = request.app.state.container
    biz = state.business
    tz = biz.tz
    days = max(1, min(days, 14))
    start = start or biz.now().date()
    t0 = datetime.combine(start, time(0), tz)
    t1 = t0 + timedelta(days=days)

    async with state.db.session() as s:
        q = select(Appointment).where(Appointment.status == "booked", Appointment.start < as_utc(t1),
                                      Appointment.end > as_utc(t0))
        if provider_id:
            q = q.where(Appointment.provider_id == provider_id)
        appts = list((await s.execute(q.order_by(Appointment.start))).scalars())
    blocks = await state.db.blocks_between(t0, t1, provider_id)

    def pname(pid):
        p = biz.provider(pid) if pid else None
        return p.name if p else "Whole clinic"

    return {
        "timezone": biz.timezone,
        "slot_minutes": biz.slot_minutes,
        "providers": [{"id": p.id, "name": p.name, "specialty": p.specialty} for p in biz.providers],
        "days": [{
            "date": (start + timedelta(days=i)).isoformat(),
            "weekday": (start + timedelta(days=i)).strftime("%a"),
            "open": [[a.strftime("%H:%M"), b.strftime("%H:%M")]
                     for a, b in biz.opening_windows(start + timedelta(days=i))],
        } for i in range(days)],
        "appointments": [{
            "id": a.id, "patient_name": a.patient_name, "phone": a.phone, "reason": a.reason,
            "provider_id": a.provider_id, "provider": pname(a.provider_id),
            "start": _local(a.start, tz), "end": _local(a.end, tz), "booked_by": "AI call" if a.call_id else "Staff",
        } for a in appts],
        "blocks": [{
            "id": b.id, "provider_id": b.provider_id, "provider": pname(b.provider_id), "reason": b.reason,
            "start": _local(b.start, tz), "end": _local(b.end, tz),
        } for b in blocks],
    }


# ------------------------------------------------------------------ blocked time
class BlockIn(BaseModel):
    date: date
    start: str = Field(examples=["13:00"])
    end: str = Field(examples=["14:00"])
    provider_id: str | None = None  # None = whole clinic
    reason: str | None = None


@router.post("/api/blocks", dependencies=admin)
async def add_block(body: BlockIn, request: Request) -> dict:
    state = request.app.state.container
    tz = state.business.tz
    s = datetime.combine(body.date, _parse_hhmm(body.start), tz)
    e = datetime.combine(body.date, _parse_hhmm(body.end), tz)
    if e <= s:
        raise HTTPException(422, "end must be after start")
    if body.provider_id and not state.business.provider(body.provider_id):
        raise HTTPException(422, "unknown provider")
    block = await state.db.add_block(s, e, body.provider_id or None, (body.reason or "").strip() or None)
    return {"id": block.id}


@router.delete("/api/blocks/{block_id}", dependencies=admin)
async def delete_block(block_id: str, request: Request) -> dict:
    if not await request.app.state.container.db.delete_block(block_id):
        raise HTTPException(404, "block not found")
    return {"deleted": True}


# ------------------------------------------------------------------ staff booking (walk-ins / phone at desk)
class BookingIn(BaseModel):
    patient_name: str = Field(min_length=1, max_length=120)
    date: date
    time: str
    provider_id: str | None = None
    phone: str | None = None
    reason: str | None = None


@router.post("/api/appointments", dependencies=admin)
async def staff_booking(body: BookingIn, request: Request) -> dict:
    state = request.app.state.container
    biz = state.business
    provider = biz.provider(body.provider_id) if body.provider_id else (biz.providers[0] if biz.providers else None)
    start = datetime.combine(body.date, _parse_hhmm(body.time), biz.tz)
    phone = None
    if body.phone and body.phone.strip():
        phone, problem = validate_phone(body.phone)
        if problem:
            raise HTTPException(422, f"Invalid phone number: {problem}")
    try:
        appt = await state.calendar.book(patient_name=body.patient_name.strip(), start=start,
                                         provider_id=provider.id if provider else None,
                                         phone=phone, reason=body.reason)
    except SlotUnavailable as exc:
        raise HTTPException(409, "That slot is not available") from exc
    return {"id": appt.id}


# ------------------------------------------------------------------ subscribable .ics feed
@router.get("/api/calendar-feed", dependencies=admin)
async def feed_info(request: Request) -> dict:
    s = request.app.state.container.settings
    url = f"{s.public_base_url.rstrip('/')}/calendar/{feed_token(s)}.ics"
    return {"url": url, "public": not any(h in url for h in ("localhost", "127.0.0.1"))}


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode())
            cur = b""
        cur += b
    parts.append(cur.decode())
    return "\r\n ".join(parts)


def _ics_dt(dt: datetime) -> str:
    return as_utc(dt).strftime("%Y%m%dT%H%M%SZ")


@router.get("/calendar/{token}.ics", include_in_schema=False)
async def ics_feed(token: str, request: Request) -> Response:
    state = request.app.state.container
    if not hmac.compare_digest(token, feed_token(state.settings)):
        raise HTTPException(404, "not found")
    biz = state.business
    now = datetime.now(UTC)
    async with state.db.session() as s:
        appts = list((await s.execute(
            select(Appointment).where(Appointment.start >= as_utc(now - timedelta(days=14)),
                                      Appointment.start <= as_utc(now + timedelta(days=90)))
            .order_by(Appointment.start))).scalars())

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//VaaniDesk//AI Receptionist//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(biz.name)} appointments", f"X-WR-TIMEZONE:{biz.timezone}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    for a in appts:
        p = biz.provider(a.provider_id)
        desc = f"Phone: {a.phone or '-'}\nReason: {a.reason or '-'}\nRef: {a.id}\nBooked by VaaniDesk"
        lines += ["BEGIN:VEVENT", f"UID:{a.id}@vaanidesk", f"DTSTAMP:{_ics_dt(now)}",
                  f"DTSTART:{_ics_dt(a.start)}", f"DTEND:{_ics_dt(a.end)}",
                  f"SUMMARY:{_ics_escape(a.patient_name + (' — ' + p.name if p else ''))}",
                  f"DESCRIPTION:{_ics_escape(desc)}",
                  f"STATUS:{'CANCELLED' if a.status == 'cancelled' else 'CONFIRMED'}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "Content-Disposition": 'inline; filename="appointments.ics"'})
