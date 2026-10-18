"""Owner dashboard REST API (HTTP Basic auth)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..db import Appointment, Call, as_utc
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
    return {
        "days": days,
        "since": since.isoformat(),
        "total_calls": len(calls),
        "failed_calls": failed,
        "booked": booked,
        "conversion_rate": round(booked / len(calls), 3) if calls else 0,
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
            out.append({"id": a.id, "patient_name": a.patient_name, "phone": a.phone, "status": a.status,
                        "provider": p.name if p else a.provider_id, "reason": a.reason, "call_id": a.call_id,
                        "start_local": as_utc(a.start).astimezone(tz).strftime("%a %d %b %Y, %I:%M %p")})
        return out


@router.post("/appointments/{appointment_id}/cancel")
async def cancel(appointment_id: str, request: Request) -> dict:
    appt = await request.app.state.container.calendar.cancel(appointment_id)
    if not appt:
        raise HTTPException(404, "appointment not found")
    return {"cancelled": True}
