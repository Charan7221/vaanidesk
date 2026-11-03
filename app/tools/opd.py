"""Walk-in OPD (outpatient department) token queue.

Indian clinics run OPD as a queue: each doctor's session (e.g. morning 09:00-13:00) hands out token
numbers 1, 2, 3, ... and patients are seen in token order. This module issues tokens (from a phone call
or the reception desk), registers patients, tracks the live queue ("now serving token 9") and estimates
each patient's waiting time. Staff move the queue with "Next patient" on the dashboard's OPD page.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..business import Business, OPDSession
from ..db import Database, OpdSessionStatus, OpdToken, Patient, utcnow

log = logging.getLogger(__name__)

ACTIVE = ("waiting", "in_consultation")
STATUSES = ("waiting", "in_consultation", "done", "skipped", "cancelled")
_SESSION_WORDS = {"morning": "morning", "afternoon": "afternoon", "evening": "evening", "night": "evening",
                  "noon": "afternoon"}


class OpdError(Exception):
    """A token can't be issued. ``reason`` and ``instruction`` go back to the agent / the dashboard."""

    def __init__(self, reason: str, instruction: str = "", **extra):
        super().__init__(reason)
        self.reason, self.instruction, self.extra = reason, instruction, extra

    def as_dict(self) -> dict:
        return {"reason": self.reason, **({"instruction": self.instruction} if self.instruction else {}),
                **self.extra}


def spoken_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


class OpdDesk:
    def __init__(self, db: Database, business: Business):
        self.db = db
        self.biz = business

    @property
    def cfg(self):
        return self.biz.opd

    @property
    def avg(self) -> timedelta:
        return timedelta(minutes=max(1, self.cfg.avg_consult_minutes))

    # ------------------------------------------------------------------ sessions
    def session(self, day: date, key: str) -> OPDSession | None:
        return next((s for s in self.biz.opd_sessions(day) if s.key == key), None)

    def closes_at(self, s: OPDSession) -> datetime:
        """When new tokens stop being issued for a session."""
        return s.end - timedelta(minutes=self.cfg.stop_before_end_minutes)

    def pick_session(self, day: date, hint: str | None, now: datetime) -> OPDSession | None:
        """Session named by the caller ("evening", "17:00"), or the next one still issuing tokens."""
        sessions = self.biz.opd_sessions(day)
        if hint:
            h = hint.strip().lower()
            word = next((v for k, v in _SESSION_WORDS.items() if k in h), None)
            for s in sessions:
                if s.key == h[:5] or s.label == word:
                    return s
            return None
        for s in sessions:
            if day != now.date() or now < self.closes_at(s):
                return s
        return None

    async def session_status(self, day: date, provider_id: str, key: str) -> OpdSessionStatus | None:
        async with self.db.session() as s:
            return (await s.execute(select(OpdSessionStatus).where(
                OpdSessionStatus.day == day.isoformat(), OpdSessionStatus.provider_id == provider_id,
                OpdSessionStatus.session == key))).scalar_one_or_none()

    # ------------------------------------------------------------------ patients
    async def register_patient(self, name: str, phone: str | None, age: int | None = None) -> Patient:
        """Find the patient (same phone + same name) or register a new one with the next patient number."""
        async with self.db.session() as s:
            if phone:
                found = (await s.execute(select(Patient).where(
                    Patient.phone == phone, func.lower(Patient.name) == name.lower()))).scalars().first()
                if found:
                    if age and found.age != age:
                        found.age = age
                        await s.commit()
                    return found
        for _ in range(5):
            async with self.db.session() as s:
                top = (await s.execute(select(func.max(Patient.number)))).scalar_one_or_none()
                patient = Patient(number=(top or 1000) + 1, name=name, phone=phone, age=age)
                s.add(patient)
                try:
                    await s.commit()
                    return patient
                except IntegrityError:  # another call took the same number: try the next one
                    await s.rollback()
        raise RuntimeError("could not allocate a patient number")

    # ------------------------------------------------------------------ tokens
    async def tokens(self, day: date, provider_id: str | None = None, key: str | None = None) -> list[OpdToken]:
        async with self.db.session() as s:
            q = select(OpdToken).where(OpdToken.day == day.isoformat())
            if provider_id:
                q = q.where(OpdToken.provider_id == provider_id)
            if key:
                q = q.where(OpdToken.session == key)
            return list((await s.execute(
                q.order_by(OpdToken.provider_id, OpdToken.session, OpdToken.number))).scalars())

    async def get(self, token_id: str) -> OpdToken | None:
        async with self.db.session() as s:
            return await s.get(OpdToken, (token_id or "").strip())

    async def find_active(self, phones: set[str], since: date) -> list[OpdToken]:
        if not phones:
            return []
        async with self.db.session() as s:
            q = select(OpdToken).where(OpdToken.phone.in_(phones), OpdToken.day >= since.isoformat(),
                                       OpdToken.status.in_(ACTIVE))
            return list((await s.execute(q.order_by(OpdToken.day, OpdToken.session))).scalars())

    async def issue(self, *, provider_id: str, day: date, patient_name: str, phone: str | None,
                    session_hint: str | None = None, age: int | None = None, reason: str | None = None,
                    source: str = "call", call_id: str | None = None,
                    now: datetime | None = None) -> tuple[OpdToken, bool]:
        """Issue the next token in a doctor's session. Returns (token, created); created is False when the
        same patient already holds a token in that session. Raises OpdError when no token can be given."""
        now = now or self.biz.now()
        today = now.date()
        provider = self.biz.provider(provider_id)
        if not self.cfg.enabled:
            raise OpdError("OPD tokens are not offered", "Offer a fixed-time appointment instead.")
        if day < today:
            raise OpdError("that date is in the past")
        if day > today + timedelta(days=self.cfg.advance_days):
            raise OpdError(f"tokens are only given for today and the next {self.cfg.advance_days} days",
                           "Offer a fixed-time appointment for that date instead (check_availability).")
        sessions = self.biz.opd_sessions(day)
        if not sessions:
            raise OpdError(f"the clinic is closed on {day.strftime('%A')}",
                           "Suggest another day. Opening hours: " + self.biz.hours_summary())
        s = self.pick_session(day, session_hint, now)
        if s is None and session_hint:
            raise OpdError(f"no {session_hint} OPD on {day.strftime('%A')}",
                           sessions=[f"{x.label} {x.key}-{x.end.strftime('%H:%M')}" for x in sessions])
        # The desk can still hand out walk-in tokens until the session ends; phone tokens stop a bit earlier.
        cutoff = (s.end if source == "staff" else self.closes_at(s)) if s else None
        if s is None or (day == today and now >= cutoff):
            later = [x for x in sessions if day == today and now < self.closes_at(x)]
            raise OpdError("tokens for that session are closed",
                           "Offer the next session or tomorrow's OPD instead.",
                           next_session_today=f"{later[0].label} from {spoken_time(later[0].start)}" if later else None)
        status = await self.session_status(day, provider.id, s.key)
        if status and status.cancelled:
            raise OpdError(f"{provider.name}'s {s.label} OPD is cancelled",
                           "Apologise, give the note if any, and offer another doctor or day.",
                           note=status.note)

        queue = await self.tokens(day, provider.id, s.key)
        for t in queue:
            if t.status in ACTIVE and t.phone == phone and t.patient_name.lower() == patient_name.lower():
                return t, False
        if sum(t.status != "cancelled" for t in queue) >= self.cfg.max_tokens_per_session:
            raise OpdError(f"{provider.name}'s {s.label} OPD is full",
                           "Offer the next session, another doctor or a fixed-time appointment.")

        patient = await self.register_patient(patient_name, phone, age)
        for _ in range(5):
            async with self.db.session() as db:
                top = (await db.execute(select(func.max(OpdToken.number)).where(
                    OpdToken.day == day.isoformat(), OpdToken.provider_id == provider.id,
                    OpdToken.session == s.key))).scalar_one_or_none()
                token = OpdToken(day=day.isoformat(), session=s.key, provider_id=provider.id, number=(top or 0) + 1,
                                 patient_id=patient.id, patient_name=patient_name, phone=phone, age=age,
                                 reason=reason, source=source, call_id=call_id)
                db.add(token)
                try:
                    await db.commit()
                    return token, True
                except IntegrityError:  # two callers at the same moment: take the next number
                    await db.rollback()
        raise RuntimeError("could not allocate a token number")

    async def patient(self, patient_id: str | None) -> Patient | None:
        if not patient_id:
            return None
        async with self.db.session() as s:
            return await s.get(Patient, patient_id)

    # ------------------------------------------------------------------ live queue
    async def position(self, token: OpdToken, now: datetime | None = None) -> dict:
        """Where a token stands: patients ahead, the number being seen now, and the estimated time."""
        now = now or self.biz.now()
        day = date.fromisoformat(token.day)
        s = self.session(day, token.session)
        queue = await self.tokens(day, token.provider_id, token.session)
        serving = next((t for t in queue if t.status == "in_consultation"), None)
        ahead = [t for t in queue if t.status == "waiting" and t.number < token.number]
        status = await self.session_status(day, token.provider_id, token.session)
        delay = timedelta(minutes=status.delay_minutes if status else 0)
        provider = self.biz.provider(token.provider_id)
        info = {"token_id": token.id, "token_number": token.number, "status": token.status,
                "patient_name": token.patient_name, "doctor": provider.name if provider else token.provider_id,
                "date": token.day, "weekday": day.strftime("%A"),
                "session": f"{s.label} {spoken_time(s.start)} to {spoken_time(s.end)}" if s else token.session,
                "now_serving": serving.number if serving else None, "patients_ahead": len(ahead)}
        if status and status.delay_minutes:
            info["doctor_running_late_minutes"] = status.delay_minutes
        if status and status.cancelled:
            info["session_cancelled"] = True
            info["note"] = status.note
        if token.status == "waiting" and s:
            base = max(now, s.start + delay)
            busy = 1 if serving else 0
            eta = base + (len(ahead) + busy) * self.avg
            report = max(s.start, eta - timedelta(minutes=15))
            info["estimated_turn"] = spoken_time(eta)
            info["please_arrive_by"] = "now" if report <= now else spoken_time(report)
        return info

    async def queue_summary(self, day: date, provider_id: str, s: OPDSession, now: datetime | None = None) -> dict:
        """One doctor's session at a glance (for the agent and the reception screen)."""
        now = now or self.biz.now()
        queue = await self.tokens(day, provider_id, s.key)
        status = await self.session_status(day, provider_id, s.key)
        serving = next((t for t in queue if t.status == "in_consultation"), None)
        waiting = [t for t in queue if t.status == "waiting"]
        issued = sum(t.status != "cancelled" for t in queue)
        cancelled = bool(status and status.cancelled)
        delay = status.delay_minutes if status else 0
        ended = day < now.date() or (day == now.date() and now >= s.end)
        accepting = (self.cfg.enabled and not cancelled and issued < self.cfg.max_tokens_per_session
                     and not (day == now.date() and now >= self.closes_at(s)) and day >= now.date()
                     and day <= now.date() + timedelta(days=self.cfg.advance_days))
        base = max(now, s.start + timedelta(minutes=delay))
        return {"session": s.key, "label": s.label, "from": s.start.strftime("%H:%M"), "to": s.end.strftime("%H:%M"),
                "now_serving": serving.number if serving else None, "waiting": len(waiting), "issued": issued,
                "delay_minutes": delay, "cancelled": cancelled, "note": status.note if status else None,
                "ended": ended, "accepting_tokens": accepting,
                "new_token_estimated_turn": spoken_time(base + (len(waiting) + (1 if serving else 0)) * self.avg)
                if accepting else None}

    # ------------------------------------------------------------------ staff actions
    async def call_next(self, day: date, provider_id: str, key: str) -> dict:
        """Finish the patient being seen and call the lowest waiting token."""
        async with self.db.session() as s:
            queue = list((await s.execute(select(OpdToken).where(
                OpdToken.day == day.isoformat(), OpdToken.provider_id == provider_id,
                OpdToken.session == key).order_by(OpdToken.number))).scalars())
            finished = None
            for t in queue:
                if t.status == "in_consultation":
                    t.status, t.done_at, finished = "done", utcnow(), t.number
            nxt = next((t for t in queue if t.status == "waiting"), None)
            if nxt:
                nxt.status, nxt.called_at = "in_consultation", utcnow()
            await s.commit()
            return {"finished": finished, "now_serving": nxt.number if nxt else None,
                    "patient_name": nxt.patient_name if nxt else None,
                    "waiting": sum(t.status == "waiting" for t in queue)}

    async def set_token_status(self, token_id: str, status: str) -> OpdToken | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        async with self.db.session() as s:
            token = await s.get(OpdToken, token_id)
            if not token:
                return None
            if status == "in_consultation":  # only one patient with the doctor at a time
                others = (await s.execute(select(OpdToken).where(
                    OpdToken.day == token.day, OpdToken.provider_id == token.provider_id,
                    OpdToken.session == token.session, OpdToken.status == "in_consultation",
                    OpdToken.id != token.id))).scalars()
                for o in others:
                    o.status, o.done_at = "done", utcnow()
                token.called_at = utcnow()
            if status == "done":
                token.done_at = utcnow()
            token.status = status
            await s.commit()
            return token

    async def set_session_status(self, day: date, provider_id: str, key: str, *, delay_minutes: int | None = None,
                                 cancelled: bool | None = None, note: str | None = None) -> OpdSessionStatus:
        async with self.db.session() as s:
            row = (await s.execute(select(OpdSessionStatus).where(
                OpdSessionStatus.day == day.isoformat(), OpdSessionStatus.provider_id == provider_id,
                OpdSessionStatus.session == key))).scalar_one_or_none()
            if row is None:
                row = OpdSessionStatus(day=day.isoformat(), provider_id=provider_id, session=key)
                s.add(row)
            if delay_minutes is not None:
                row.delay_minutes = max(0, int(delay_minutes))
            if cancelled is not None:
                row.cancelled = bool(cancelled)
            if note is not None:
                row.note = note.strip() or None
            await s.commit()
            return row
