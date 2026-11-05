"""OPD reception screen API: today's token queues per doctor, walk-in tokens, "Next patient", delays."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import audit
from ..db import as_utc
from ..phone import validate_phone
from ..security import require_admin
from ..tools.opd import STATUSES, OpdError

router = APIRouter(prefix="/api/opd", tags=["opd"], dependencies=[Depends(require_admin)])


def _state(request: Request):
    return request.app.state.container


def _provider(state, provider_id: str):
    p = state.business.provider(provider_id) if provider_id else None
    if not p:
        raise HTTPException(422, "unknown doctor")
    return p


def _session(state, day: date, key: str):
    s = state.tools.opd.session(day, key)
    if not s:
        raise HTTPException(422, f"no OPD session at {key} on {day.isoformat()}")
    return s


@router.get("")
async def board(request: Request, day: date | None = None) -> dict:
    """Every doctor's OPD sessions for a day, with the full token list (staff only: includes names)."""
    state = _state(request)
    biz, opd = state.business, state.tools.opd
    day = day or biz.now().date()
    tokens = await opd.tokens(day)
    doctors = []
    for p in biz.providers:
        sessions = []
        for s in biz.opd_sessions(day):
            summary = await opd.queue_summary(day, p.id, s)
            summary["tokens"] = [{
                "id": t.id, "number": t.number, "patient_name": t.patient_name, "phone": t.phone, "age": t.age,
                "reason": t.reason, "status": t.status, "source": t.source,
                "fee_type": t.fee_type, "fee_amount": t.fee_amount, "paid": bool(t.paid),
                "created_at": as_utc(t.created_at).astimezone(biz.tz).strftime("%H:%M"),
            } for t in tokens if t.provider_id == p.id and t.session == s.key]
            sessions.append(summary)
        dept = biz.department_of(p.id)
        doctors.append({"id": p.id, "name": p.name, "specialty": p.specialty, "sessions": sessions,
                        "department": dept.name if dept else None})
    return {"date": day.isoformat(), "weekday": day.strftime("%A"), "enabled": biz.opd.enabled,
            "avg_consult_minutes": biz.opd.avg_consult_minutes, "currency": biz.fees.currency, "doctors": doctors}


class TokenIn(BaseModel):
    provider_id: str
    patient_name: str = Field(min_length=2, max_length=120)
    day: date | None = None
    session: str | None = None
    phone: str | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    reason: str | None = None


@router.post("/tokens")
async def walk_in(body: TokenIn, request: Request) -> dict:
    state = _state(request)
    p = _provider(state, body.provider_id)
    phone = None
    if body.phone and body.phone.strip():
        phone, problem = validate_phone(body.phone)
        if problem:
            raise HTTPException(422, f"Invalid phone number: {problem}")
    try:
        token, created = await state.tools.opd.issue(
            provider_id=p.id, day=body.day or state.business.now().date(), patient_name=body.patient_name.strip(),
            phone=phone, session_hint=body.session, age=body.age, reason=body.reason, source="staff")
    except OpdError as exc:
        raise HTTPException(409, exc.reason) from exc
    await audit(request, "opd.walk_in", f"token {token.number}", doctor=p.id, patient=token.patient_name)
    return {"id": token.id, "number": token.number, "created": created, "fee_type": token.fee_type,
            "fee_amount": token.fee_amount}


class QueueIn(BaseModel):
    provider_id: str
    session: str
    day: date | None = None


@router.post("/next")
async def call_next(body: QueueIn, request: Request) -> dict:
    state = _state(request)
    p = _provider(state, body.provider_id)
    day = body.day or state.business.now().date()
    _session(state, day, body.session)
    result = await state.tools.opd.call_next(day, p.id, body.session)
    await audit(request, "opd.next", p.id, session=body.session, now_serving=result["now_serving"])
    return result


class StatusIn(BaseModel):
    status: str


@router.post("/tokens/{token_id}/status")
async def token_status(token_id: str, body: StatusIn, request: Request) -> dict:
    if body.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    token = await _state(request).tools.opd.set_token_status(token_id, body.status)
    if not token:
        raise HTTPException(404, "token not found")
    await audit(request, "opd.token_status", f"token {token.number}", status=token.status)
    return {"id": token.id, "number": token.number, "status": token.status}


class PaidIn(BaseModel):
    paid: bool = True


@router.post("/tokens/{token_id}/paid")
async def mark_paid(token_id: str, body: PaidIn, request: Request) -> dict:
    token = await _state(request).tools.opd.set_paid(token_id, body.paid)
    if not token:
        raise HTTPException(404, "token not found")
    await audit(request, "opd.paid" if body.paid else "opd.unpaid", f"token {token.number}", amount=token.fee_amount)
    return {"id": token.id, "paid": bool(token.paid)}


class SessionStatusIn(BaseModel):
    provider_id: str
    day: date | None = None
    session: str | None = None  # None = every session that day
    delay_minutes: int | None = Field(default=None, ge=0, le=600)
    cancelled: bool | None = None
    note: str | None = Field(default=None, max_length=255)


@router.post("/session-status")
async def session_status(body: SessionStatusIn, request: Request) -> dict:
    state = _state(request)
    p = _provider(state, body.provider_id)
    day = body.day or state.business.now().date()
    keys = [body.session] if body.session else [s.key for s in state.business.opd_sessions(day)]
    for key in keys:
        _session(state, day, key)
        await state.tools.opd.set_session_status(day, p.id, key, delay_minutes=body.delay_minutes,
                                                 cancelled=body.cancelled, note=body.note)
    await audit(request, "opd.session_status", p.id, sessions=keys,
                **body.model_dump(exclude_none=True, exclude={"provider_id", "day", "session"}))
    return {"updated": keys}
