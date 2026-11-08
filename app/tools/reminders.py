"""Day-before reminders for appointments and OPD tokens, in the language the patient spoke on the call.

Runs every few minutes; after the clinic's ``reminders.send_at`` time (default 6 PM) it messages everyone
booked for tomorrow who hasn't had a reminder yet. Sent by WhatsApp when configured, else SMS. Each booking
is reminded at most once (``reminder_sent_at``), so restarts and retries don't send duplicates.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from ..business import Business
from ..db import Appointment, Call, Database, OpdToken, as_utc, utcnow
from ..messaging import Message

log = logging.getLogger(__name__)

TEMPLATES = {
    "en-IN": {
        "appt": "{clinic}: Reminder - {name} has an appointment with {doctor} tomorrow ({day}) at {time}. "
                "To cancel or change, call {phone}.",
        "token": "{clinic}: Reminder - {name} has OPD token {number} with {doctor} tomorrow ({day}), {session}. "
                 "To cancel, call {phone}.",
    },
    "hi-IN": {
        "appt": "{clinic}: याद दिलाना - {name} की {doctor} के साथ कल ({day}) {time} बजे अपॉइंटमेंट है। "
                "रद्द या बदलने के लिए {phone} पर कॉल करें।",
        "token": "{clinic}: याद दिलाना - {name} का कल ({day}) {doctor} के पास OPD टोकन नंबर {number} है, {session}। "
                 "रद्द करने के लिए {phone} पर कॉल करें।",
    },
    "te-IN": {
        "appt": "{clinic}: గుర్తుచేస్తున్నాం - {name} కి రేపు ({day}) {time} కి {doctor} గారితో అపాయింట్‌మెంట్ ఉంది. "
                "రద్దు లేదా మార్చడానికి {phone} కి కాల్ చేయండి.",
        "token": "{clinic}: గుర్తుచేస్తున్నాం - {name} కి రేపు ({day}) {doctor} గారి దగ్గర OPD టోకెన్ నంబర్ {number}, "
                 "{session}. రద్దు చేయడానికి {phone} కి కాల్ చేయండి.",
    },
}


def due(biz: Business, now: datetime) -> bool:
    """Is it time to send tomorrow's reminders?"""
    try:
        at = time.fromisoformat(biz.reminders.send_at)
    except ValueError:
        at = time(18, 0)
    return biz.reminders.enabled and now.time() >= at


async def _languages(db: Database, call_ids: set[str]) -> dict[str, str]:
    if not call_ids:
        return {}
    async with db.session() as s:
        rows = (await s.execute(select(Call.id, Call.language).where(Call.id.in_(call_ids)))).all()
    return {cid: lang for cid, lang in rows if lang}


async def send_reminders(db: Database, biz: Business, notifier, now: datetime | None = None,
                         force: bool = False) -> dict:
    """Send tomorrow's reminders for the current clinic. Returns counts (sent / failed / pending)."""
    now = now or biz.now()
    out = {"sent": 0, "failed": 0, "skipped_no_phone": 0}
    if not force and not due(biz, now):
        return out
    if not notifier.enabled:
        out["not_configured"] = True
        return out
    tomorrow: date = now.date() + timedelta(days=1)
    start = datetime.combine(tomorrow, time(0), biz.tz)
    async with db.session() as s:
        appts = list((await s.execute(select(Appointment).where(
            Appointment.status == "booked", Appointment.reminder_sent_at.is_(None),
            Appointment.start >= as_utc(start), Appointment.start < as_utc(start + timedelta(days=1)),
            (Appointment.confirmed.is_(None)) | (Appointment.confirmed.is_(True))))).scalars())
        tokens = list((await s.execute(select(OpdToken).where(
            OpdToken.day == tomorrow.isoformat(), OpdToken.status == "waiting", OpdToken.reminder_sent_at.is_(None),
            (OpdToken.confirmed.is_(None)) | (OpdToken.confirmed.is_(True))))).scalars())
    langs = await _languages(db, {x.call_id for x in [*appts, *tokens] if x.call_id})
    common = {"clinic": biz.name, "phone": biz.phone or biz.handoff_number, "day": tomorrow.strftime("%a %d %b")}

    def doctor(pid):
        p = biz.provider(pid)
        return p.name if p else "the doctor"

    jobs = []
    for a in appts:
        local = as_utc(a.start).astimezone(biz.tz)
        jobs.append((Appointment, a, "appt", {"name": a.patient_name, "doctor": doctor(a.provider_id),
                                              "time": local.strftime("%I:%M %p").lstrip("0")}))
    for t in tokens:
        sess = next((x for x in biz.opd_sessions(tomorrow) if x.key == t.session), None)
        when = f"{sess.label} OPD from {sess.start.strftime('%I:%M %p').lstrip('0')}" if sess else t.session
        jobs.append((OpdToken, t, "token", {"name": t.patient_name, "doctor": doctor(t.provider_id),
                                            "number": t.number, "session": when}))
    for model, item, kind, fields in jobs:
        if not item.phone and not item.email:
            out["skipped_no_phone"] += 1
            continue
        lang = langs.get(item.call_id or "", biz.languages[0] if biz.languages else "en-IN")
        text = TEMPLATES.get(lang, TEMPLATES["en-IN"])[kind].format(**common, **fields)
        title = "Reminder: your visit tomorrow"
        msg = Message(text=text, subject=f"{title} · {biz.name}", title=title, lines=[
            ("Patient", fields["name"]), ("Doctor", fields["doctor"]), ("Day", common["day"]),
            ("Time", fields.get("time", "")), ("Token", str(fields.get("number", "")) if kind == "token" else ""),
            ("Session", fields.get("session", "")), ("Clinic", biz.name), ("Address", biz.address)],
            footer=f"To cancel or change, call {common['phone']}.")
        ok = bool((await notifier.notify_patient(biz, kind="reminder", msg=msg, phone=item.phone,
                                                 email=item.email))["sent"])
        if ok:
            async with db.session() as s:
                row = await s.get(model, item.id)
                row.reminder_sent_at = utcnow()
                await s.commit()
            out["sent"] += 1
        else:
            out["failed"] += 1
    if out["sent"] or out["failed"]:
        log.info("Reminders for %s: %s", tomorrow, out)
    return out
