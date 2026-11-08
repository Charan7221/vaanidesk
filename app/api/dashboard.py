"""Owner dashboard REST API (HTTP Basic auth)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..auth import audit
from ..db import Appointment, Call, OpdToken, as_utc
from ..messaging import Message, valid_email
from ..phone import validate_phone
from ..security import require_admin

router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_admin)])


def _iso(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt else None


def _call_dict(c: Call) -> dict:
    duration = (as_utc(c.ended_at) - as_utc(c.started_at)).total_seconds() if c.ended_at else None
    return {
        "id": c.id, "channel": c.channel, "caller": c.caller, "language": c.language, "status": c.status,
        "outcome": c.outcome, "summary": c.summary, "avg_latency_ms": round(c.avg_latency_ms) if c.avg_latency_ms else None,
        "turns": c.turns_count, "interruptions": c.interruptions, "started_at": _iso(c.started_at),
        "duration_s": round(duration) if duration is not None else None,
    }


def _since(request: Request, days: int) -> datetime:
    """Start of the reporting period: 'Today' = since local midnight, else the last N days."""
    biz = request.app.state.container.business
    if days <= 1:
        now = biz.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.now(UTC) - timedelta(days=days)


def _is_dead(c: Call, active: dict) -> bool:
    """A call that never finished and isn't live (e.g. the server crashed while it started)."""
    return c.status == "failed" or (c.ended_at is None and c.id not in active)


@router.get("/stats")
async def stats(request: Request, days: int = 7) -> dict:
    container = request.app.state.container
    db = container.db
    since = _since(request, days)
    async with db.session() as s:
        calls = list((await s.execute(select(Call).where(Call.started_at >= as_utc(since)))).scalars())
    failed = sum(1 for c in calls if _is_dead(c, container.active_sessions))
    calls = [c for c in calls if not _is_dead(c, container.active_sessions)]
    async with db.session() as s:
        upcoming = (await s.execute(select(func.count()).select_from(Appointment).where(
            Appointment.status == "booked", Appointment.start >= datetime.now(UTC)))).scalar_one()
    latencies = [c.avg_latency_ms for c in calls if c.avg_latency_ms]
    by_lang: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for c in calls:
        lang = c.language or "not detected"
        by_lang[lang] = by_lang.get(lang, 0) + 1
        by_outcome[c.outcome or "in progress"] = by_outcome.get(c.outcome or "in progress", 0) + 1
    booked = by_outcome.get("booked", 0)
    tokens = by_outcome.get("token", 0)  # calls that ended with an OPD token
    return {
        "days": days,
        "since": since.isoformat(),
        "total_calls": len(calls),
        "failed_calls": failed,
        "booked": booked,
        "opd_tokens": tokens,
        "conversion_rate": round((booked + tokens) / len(calls), 3) if calls else 0,
        "transferred": by_outcome.get("transferred", 0),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "upcoming_appointments": upcoming,
        "by_language": by_lang,
        "by_outcome": by_outcome,
        "active_calls": len(request.app.state.container.active_sessions),
    }


@router.get("/calls")
async def list_calls(request: Request, limit: int = 50, days: int | None = None) -> list[dict]:
    container = request.app.state.container
    async with container.db.session() as s:
        q = select(Call)
        if days:
            q = q.where(Call.started_at >= as_utc(_since(request, days)))
        rows = (await s.execute(q.order_by(Call.started_at.desc()).limit(min(limit, 200)))).scalars()
        out = []
        for c in rows:
            d = _call_dict(c)
            if _is_dead(c, container.active_sessions):
                d["status"], d["outcome"] = "failed", "failed"
            out.append(d)
        return out


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request) -> dict:
    db = request.app.state.container.db
    async with db.session() as s:
        call = (await s.execute(select(Call).options(selectinload(Call.turns)).where(Call.id == call_id))).scalar_one_or_none()
        if not call:
            raise HTTPException(404, "call not found")
        data = _call_dict(call)
        data["turns"] = [{"role": t.role, "text": t.text, "language": t.language,
                          "latency_ms": round(t.latency_ms) if t.latency_ms else None, "meta": t.meta,
                          "at": _iso(t.created_at)} for t in call.turns]
        return data


@router.get("/appointments")
async def list_appointments(request: Request, upcoming: bool = True) -> list[dict]:
    state = request.app.state.container
    tz = state.business.tz
    async with state.db.session() as s:
        q = select(Appointment).order_by(Appointment.start)
        if upcoming:
            q = q.where(Appointment.start >= datetime.now(UTC) - timedelta(hours=1))
        rows = (await s.execute(q.limit(200))).scalars()
        out = []
        for a in rows:
            p = state.business.provider(a.provider_id)
            out.append({"id": a.id, "patient_name": a.patient_name, "phone": a.phone, "email": a.email,
                        "status": a.status,
                        "provider": p.name if p else a.provider_id, "reason": a.reason, "call_id": a.call_id,
                        "start_local": as_utc(a.start).astimezone(tz).strftime("%a %d %b %Y, %I:%M %p")})
        return out


@router.post("/appointments/{appointment_id}/cancel")
async def cancel(appointment_id: str, request: Request) -> dict:
    appt = await request.app.state.container.calendar.cancel(appointment_id)
    if not appt:
        raise HTTPException(404, "appointment not found")
    await audit(request, "appointment.cancel", appointment_id, patient=appt.patient_name)
    return {"cancelled": True}


# ------------------------------------------------------------------ needs attention: follow-ups + pilot confirmations
@router.get("/attention")
async def attention(request: Request) -> dict:
    """What staff must act on: open follow-ups and (pilot mode) AI bookings/tokens awaiting confirmation."""
    state = request.app.state.container
    biz, tz = state.business, state.business.tz
    follow = [{"id": f.id, "kind": f.kind, "phone": f.phone, "patient_name": f.patient_name, "reason": f.reason,
               "call_id": f.call_id, "at": as_utc(f.created_at).astimezone(tz).strftime("%d %b, %I:%M %p")}
              for f in await state.db.follow_ups("open")]
    async with state.db.session() as s:
        appts = (await s.execute(select(Appointment).where(Appointment.confirmed.is_(False),
                                                           Appointment.status == "booked")
                                 .order_by(Appointment.start))).scalars()
        tokens = (await s.execute(select(OpdToken).where(OpdToken.confirmed.is_(False),
                                                         OpdToken.status.in_(("waiting", "in_consultation")))
                                  .order_by(OpdToken.day, OpdToken.number))).scalars()
        pending = [{"kind": "appointment", "id": a.id, "patient_name": a.patient_name, "phone": a.phone,
                    "doctor": (biz.provider(a.provider_id).name if biz.provider(a.provider_id) else a.provider_id),
                    "when": as_utc(a.start).astimezone(tz).strftime("%a %d %b, %I:%M %p"), "call_id": a.call_id}
                   for a in appts]
        pending += [{"kind": "token", "id": t.id, "patient_name": t.patient_name, "phone": t.phone,
                     "doctor": (biz.provider(t.provider_id).name if biz.provider(t.provider_id) else t.provider_id),
                     "when": f"Token {t.number}, {t.day} {t.session}", "call_id": t.call_id} for t in tokens]
    return {"pilot_mode": biz.pilot_mode, "follow_ups": follow, "pending": pending,
            "setup_warnings": setup_warnings(biz, state.notifier.status())}


PLACEHOLDER_NUMBERS = {"+919000000000", "+919000000001"}  # the example numbers in the clinic templates


def setup_warnings(biz, channels: dict) -> list[str]:
    """Things in the clinic's setup that will make calls or messages fail."""
    out = []
    if biz.handoff_number in PLACEHOLDER_NUMBERS or not biz.handoff_number:
        out.append("The transfer / staff number is still the example number. Callers who ask for a person and "
                   "follow-up alerts go nowhere: set it in Setup → Clinic → 'Transfer calls to'.")
    if not (channels.get("whatsapp") or channels.get("sms")):
        out.append("No WhatsApp or SMS provider is set up, so patients don't get booking confirmations or reminders "
                   "(see docs/GO_LIVE.md Step 5). They are listed under Messages to patients as \"Not set up\".")
    return out


class ResolveIn(BaseModel):
    note: str | None = None


@router.post("/follow-ups/{item_id}/done")
async def follow_up_done(item_id: str, body: ResolveIn, request: Request) -> dict:
    if not await request.app.state.container.db.resolve_follow_up(item_id, body.note):
        raise HTTPException(404, "follow-up not found")
    await audit(request, "follow_up.done", item_id, note=body.note)
    return {"done": True}


class DecisionIn(BaseModel):
    approve: bool


@router.post("/pending/{kind}/{item_id}")
async def decide(kind: str, item_id: str, body: DecisionIn, request: Request) -> dict:
    """Pilot mode: staff approve (patient gets the confirmation SMS) or reject (cancelled, patient told to call)."""
    state = request.app.state.container
    biz = state.business
    if kind == "appointment":
        appt = await state.db.get_appointment(item_id)
        if not appt or appt.status != "booked":
            raise HTTPException(404, "appointment not found")
        await state.db.update_appointment(item_id, confirmed=body.approve)
        if not body.approve:
            await state.calendar.cancel(item_id)
        name, phone, email = appt.patient_name, appt.phone, appt.email
        what = f"appointment on {as_utc(appt.start).astimezone(biz.tz).strftime('%a %d %b, %I:%M %p')}"
    elif kind == "token":
        token = await state.tools.opd.set_confirmed(item_id, body.approve)
        if not token:
            raise HTTPException(404, "token not found")
        name, phone, email = token.patient_name, token.phone, token.email
        what = f"OPD token {token.number} on {token.day}"
    else:
        raise HTTPException(422, "kind must be appointment or token")
    if phone or email:
        text = (f"{biz.name}: your {what} for {name} is confirmed." if body.approve else
                f"{biz.name}: sorry, we couldn't confirm your {what} for {name}. Please call us on {biz.phone}.")
        head = "Booking confirmed" if body.approve else "We couldn't confirm your booking"
        await state.notifier.notify_patient(biz, kind="pilot", phone=phone, email=email, msg=Message(
            text=text, subject=f"{head} · {biz.name}", title=head, lines=[("Patient", name), ("Booking", what),
                                                                         ("Clinic", biz.name), ("Phone", biz.phone)]))
    await audit(request, "pilot.confirm" if body.approve else "pilot.reject", f"{kind}:{item_id}", patient=name)
    return {"approved": body.approve}


# ------------------------------------------------------------------ messages to patients
@router.get("/messages")
async def message_log(request: Request, limit: int = 50) -> dict:
    """Recent WhatsApp / SMS / email messages and which channels are set up."""
    state = request.app.state.container
    tz = state.business.tz
    rows = await state.db.messages(min(max(limit, 1), 200))
    return {"channels": state.notifier.status(), "messages": [{
        "at": as_utc(m.at).astimezone(tz).strftime("%d %b, %I:%M %p"), "kind": m.kind, "channel": m.channel,
        "to": m.to, "status": m.status, "error": m.error, "preview": m.preview} for m in rows]}


class MessageIn(BaseModel):
    phone: str | None = None
    email: str | None = None
    text: str
    subject: str | None = None


@router.post("/messages")
async def send_message(body: MessageIn, request: Request) -> dict:
    """Staff write to a patient (e.g. "the doctor is running late"). WhatsApp first, else SMS; and/or email."""
    state = request.app.state.container
    biz = state.business
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(422, "Write a message")
    if len(text) > 1000:
        raise HTTPException(422, "Keep the message under 1000 characters")
    phone = email = None
    if body.phone and body.phone.strip():
        phone, problem = validate_phone(body.phone)
        if problem:
            raise HTTPException(422, f"Phone number: {problem}")
    if body.email and body.email.strip():
        email = valid_email(body.email)
        if not email:
            raise HTTPException(422, "That email address doesn't look right")
    if not (phone or email):
        raise HTTPException(422, "Add a phone number or an email address")
    subject = (body.subject or "").strip() or f"Message from {biz.name}"
    result = await state.notifier.send(
        kind="manual", phone=phone, email=email, phone_channels=biz.notifications.phone_channels,
        msg=Message(text=f"{biz.name}: {text}", subject=subject, title=subject,
                    footer=f"{biz.name} · {biz.phone}".strip(" ·")))
    await audit(request, "message.send", phone or email, channels=result["sent"], failed=result["failed"])
    return result
