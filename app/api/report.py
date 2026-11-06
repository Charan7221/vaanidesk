"""Results report for the clinic owner: what VaaniDesk did for the clinic in a period.

The numbers a clinic pays for: calls answered (including the ones the front desk would have missed after
hours or while already on a call), bookings and tokens made by the AI, the consultation fees they bring in,
staff time saved, and reminders sent.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from ..db import Appointment, Call, FollowUp, OpdToken, as_utc
from ..security import require_admin

router = APIRouter(prefix="/api", tags=["report"], dependencies=[Depends(require_admin)])

RESOLVED = {"booked", "token", "cancelled", "info"}  # the AI finished the call without staff


def _open_at(biz, local: datetime) -> bool:
    return any(start <= local < end for start, end in biz.opening_windows(local.date()))


@router.get("/report")
async def report(request: Request, days: int = 30) -> dict:
    state = request.app.state.container
    biz, db = state.business, state.db
    days = max(1, min(days, 366))
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    cur = biz.fees.currency

    async with db.session() as s:
        calls = list((await s.execute(select(Call).where(Call.started_at >= since).order_by(Call.started_at))).scalars())
        appts = list((await s.execute(select(Appointment).where(Appointment.created_at >= since,
                                                                Appointment.call_id.is_not(None)))).scalars())
        tokens = list((await s.execute(select(OpdToken).where(OpdToken.created_at >= since,
                                                              OpdToken.source == "call"))).scalars())
        reminded = len(list((await s.execute(select(Appointment.id).where(Appointment.reminder_sent_at >= since)))
                            .scalars())) + len(list((await s.execute(select(OpdToken.id).where(
                                OpdToken.reminder_sent_at >= since))).scalars()))
        follow = list((await s.execute(select(FollowUp).where(FollowUp.created_at >= since))).scalars())

    calls = [c for c in calls if c.status != "failed" and c.ended_at is not None]
    outcomes = Counter(c.outcome or "info" for c in calls)
    seconds = [(as_utc(c.ended_at) - as_utc(c.started_at)).total_seconds() for c in calls]
    after_hours, overlapping, by_day, by_hour = set(), set(), Counter(), Counter()
    active_until: list[datetime] = []
    for c in calls:
        start, end = as_utc(c.started_at), as_utc(c.ended_at)
        local = start.astimezone(biz.tz)
        by_day[local.date().isoformat()] += 1
        by_hour[local.hour] += 1
        if not _open_at(biz, local):
            after_hours.add(c.id)
        active_until = [e for e in active_until if e > start]
        if active_until:  # another call was still going on: one receptionist couldn't take both
            overlapping.add(c.id)
        active_until.append(end)

    handled = [c for c, sec in zip(calls, seconds, strict=True) if (c.outcome or "info") in RESOLVED]
    handled_minutes = sum(sec for c, sec in zip(calls, seconds, strict=True) if (c.outcome or "info") in RESOLVED) / 60
    booked = [a for a in appts if a.status == "booked"]
    live_tokens = [t for t in tokens if t.status != "cancelled"]
    revenue = sum(a.fee_amount or 0 for a in booked) + sum(t.fee_amount or 0 for t in live_tokens)
    langs = Counter(c.language or "unknown" for c in calls)
    first_day = (until - timedelta(days=days - 1)).astimezone(biz.tz).date()
    return {
        "clinic": biz.name, "days": days, "since": since.isoformat(), "until": until.isoformat(), "currency": cur,
        "calls_answered": len(calls),
        "would_have_missed": len(after_hours | overlapping),
        "after_hours_calls": len(after_hours),
        "calls_while_another_call_was_on": len(overlapping),
        "handled_without_staff": len(handled),
        "handled_without_staff_rate": round(len(handled) / len(calls), 3) if calls else 0,
        "staff_minutes_saved": round(handled_minutes),
        "avg_call_seconds": round(sum(seconds) / len(seconds)) if seconds else 0,
        "appointments_booked_by_ai": len(booked),
        "opd_tokens_by_ai": len(live_tokens),
        "free_follow_ups": sum(1 for x in [*booked, *live_tokens] if x.fee_type == "follow_up" and not x.fee_amount),
        "consultation_fees_from_ai_bookings": revenue,
        "cancellations_handled": outcomes.get("cancelled", 0),
        "transferred_to_staff": outcomes.get("transferred", 0),
        "callbacks_requested": outcomes.get("callback", 0),
        "reminders_sent": reminded,
        "follow_ups": {"total": len(follow), "open": sum(f.status == "open" for f in follow)},
        "languages": dict(langs.most_common()),
        "calls_by_day": [{"date": (first_day + timedelta(days=i)).isoformat(),
                          "calls": by_day.get((first_day + timedelta(days=i)).isoformat(), 0)} for i in range(days)],
        "calls_by_hour": [{"hour": h, "calls": by_hour.get(h, 0)} for h in range(24)],
    }


@router.post("/reminders/send-now")
async def send_now(request: Request) -> dict:
    """Send tomorrow's reminders immediately (normally automatic in the evening)."""
    from ..auth import audit
    from ..tools.reminders import send_reminders
    state = request.app.state.container
    result = await send_reminders(state.db, state.business, state.twilio, force=True)
    await audit(request, "reminders.send_now", **result)
    return result
