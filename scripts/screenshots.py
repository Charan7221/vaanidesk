"""Regenerate the README screenshots: start VaaniDesk with a sample clinic and mocked AI providers, fill it
with realistic demo data, and photograph every page with a headless browser.

    pip install playwright && playwright install chromium     # once
    python -m scripts.screenshots                             # writes docs/screenshots/*.png

No API keys are used and nothing is sent anywhere: the LLM, speech and messaging providers are fakes, and the
data lives in a temporary database. Latency numbers in the pictures are sample values, not measurements.
"""
from __future__ import annotations

import asyncio
import random
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

import uvicorn
from sqlalchemy import update

from app.api.accounts import create_user, new_clinic
from app.business import Business
from app.config import ROOT_DIR, Settings
from app.db import Appointment, Call, as_utc, current_clinic
from app.main import create_app
from app.state import AppState
from tests.fakes import FakeLLM, FakeSTT, FakeTTS, text, tool

OUT = ROOT_DIR / "docs" / "screenshots"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
OWNER = ("priya@sunriseclinic.in", "sunrise-owner-2026")
ADMIN = ("admin", "screenshot-admin-pass")
random.seed(7)


class SentChannel:
    """A messaging channel that is 'set up' and always delivers (nothing leaves the machine)."""

    def enabled(self):
        return True

    async def send(self, to, msg):
        return True, None


def demo_llm(today: date) -> FakeLLM:
    tomorrow = (today + timedelta(days=1)).isoformat()
    return FakeLLM([
        tool("check_availability", "c1", date=tomorrow, part_of_day="evening"),
        text("Tomorrow evening Dr. Srinivasa Rao is free from five o'clock and Dr. Lakshmi Devi from five twenty. "
             "Which doctor and time would you like?"),
        tool("book_appointment", "c2", patient_name="Sai Charan", date=tomorrow, time="17:20",
             provider_id="dr-rao", phone="9000012345", reason="fever and body pain"),
        text("Your appointment with Dr. Srinivasa Rao is requested for tomorrow at five twenty in the evening. "
             "The consultation fee is four hundred rupees. The clinic will confirm it on WhatsApp shortly."),
        tool("get_opd_token", "c3", provider_id="dr-lakshmi", patient_name="Aarav", age=6, reason="cough",
             phone="9000012345"),
        text("Aarav has token 4 for Dr. Lakshmi Devi's morning OPD, waiting for the clinic's confirmation. "
             "Please come by ten forty-five; the time is an estimate."),
    ], delay=0.25)


# ------------------------------------------------------------------ sample data
CALLERS = [f"+9190000{n:05d}" for n in (10231, 10457, 11873, 12094, 12562, 13308, 13741, 14205, 14699, 15130)]
SUMMARIES = {
    "booked": ["Booked Dr. Rao for fever, tomorrow 5:20 PM.", "Booked a paediatrics visit for a 4-year-old with cough.",
               "Moved an appointment from Monday to Wednesday evening.", "Booked a follow-up for knee pain."],
    "token": ["Gave OPD token 7 for Dr. Lakshmi Devi's evening OPD.", "Gave a morning OPD token for a sugar test."],
    "info": ["Asked about fasting before a sugar test (answered from lab-tests.md).",
             "Asked if cashless insurance is accepted.", "Asked for clinic timings on Sunday."],
    "callback": ["Wants a call back about a home visit for an elderly parent."],
    "transferred": ["Chest pain: told to call 108 and transferred to staff."],
    "cancelled": ["Cancelled tomorrow's appointment (travelling)."],
}
LANGS = ["te-IN"] * 5 + ["hi-IN"] * 3 + ["en-IN"] * 4


async def seed(state: AppState, today: date) -> None:
    db, tz = state.db, state.business.tz
    current_clinic.set("default")
    await create_user(state, "default", OWNER[0], "Dr. Priya Reddy", "owner", OWNER[1])
    await create_user(state, "default", "reception@sunriseclinic.in", "Lakshmi (front desk)", "staff", "front-desk-2026")
    await new_clinic(state, "sai-ortho", "Sai Ortho & Physio", "clinic", "owner@saiortho.in", "sai-ortho-owner-1")
    await new_clinic(state, "city-care", "City Care Multispeciality Hospital", "hospital", "admin@citycare.in",
                     "city-care-admin-1")
    current_clinic.set("default")

    # calls over the last 14 days (more in the evening and after hours)
    now = datetime.combine(today, time(10, 30), tz)
    outcomes = ["booked"] * 9 + ["token"] * 5 + ["info"] * 6 + ["callback"] * 2 + ["transferred", "cancelled"]
    for i in range(46):
        day = today - timedelta(days=random.choice([0, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]))
        hour = random.choice([7, 8, 9, 10, 11, 12, 13, 14, 17, 18, 19, 20, 21, 22])
        start = datetime.combine(day, time(hour, random.randint(0, 59)), tz)
        if start >= now:
            start = now - timedelta(minutes=random.randint(30, 600))
        outcome = random.choice(outcomes)
        call = await db.create_call(channel="phone" if i % 5 else "browser", caller=random.choice(CALLERS))
        start = as_utc(start)  # the database keeps UTC
        await db.finish_call(call.id, started_at=start, ended_at=start + timedelta(seconds=random.randint(55, 210)),
                             status="transferred" if outcome == "transferred" else "completed", outcome=outcome,
                             language=random.choice(LANGS), summary=random.choice(SUMMARIES[outcome]),
                             avg_latency_ms=random.randint(780, 1350), turns_count=random.randint(4, 12))
        if outcome == "booked" and day < today - timedelta(days=1):  # past visits booked by the AI
            visit = datetime.combine(day + timedelta(days=1), time(random.choice([9, 10, 11, 17, 18]),
                                                                    random.choice([0, 20, 40])), tz)
            a = await db.add_appointment(patient_name=random.choice(["Ravi Kumar", "Sita", "Anjali", "Mahesh", "Fatima"]),
                                         phone=call.caller, provider_id=random.choice(["dr-rao", "dr-lakshmi"]),
                                         start=visit, end=visit + timedelta(minutes=20), call_id=call.id)
            await db.update_appointment(a.id, fee_type="new", fee_amount=400, status="booked")

    # one call with a full transcript (opened in the call dialog)
    call = await db.create_call(channel="phone", caller="+919000012345")
    turns = [("assistant", "నమస్కారం! Sunrise Family Clinic కి కాల్ చేసినందుకు ధన్యవాదాలు. నేను Vaani. మీకు ఎలా సహాయం చేయగలను?", None),
             ("user", "రేపు సాయంత్రం డాక్టర్ అపాయింట్‌మెంట్ కావాలి", None),
             ("tool", "check_availability", {"args": {"part_of_day": "evening"}, "result": {"available": ["17:00", "17:20"]}}),
             ("assistant", "డాక్టర్ రావు గారు 5:00 లేదా 5:20 కి ఖాళీగా ఉన్నారు. ఏది సరిపోతుంది?", None),
             ("user", "5:20 కి. నా పేరు సాయి చరణ్.", None),
             ("tool", "book_appointment", {"args": {"time": "17:20"}, "result": {"booked": True}}),
             ("assistant", "బుక్ అయింది! రేపు సాయంత్రం 5:20 కి డాక్టర్ రావు గారితో. వివరాలు WhatsApp లో పంపాము.", None)]
    for role, txt, meta in turns:
        await db.add_turn(call.id, role, txt, language="te-IN", meta=meta,
                          latency_ms=random.randint(850, 1150) if role == "assistant" else None)
    start = as_utc(now - timedelta(minutes=25))
    await db.finish_call(call.id, started_at=start, ended_at=start + timedelta(seconds=96), status="completed",
                         outcome="booked", language="te-IN", avg_latency_ms=942, turns_count=7,
                         summary="Booked Dr. Rao for tomorrow 5:20 PM (Telugu).")
    state.transcript_call_id = call.id

    # this week's appointments (calendar + OPD queue), some waiting for staff in pilot mode
    week = [today + timedelta(days=d) for d in range(0, 6)]
    names = ["Venkat Rao", "Swathi", "Imran Khan", "Padma", "Kiran", "Deepika", "Suresh", "Meena", "Arjun", "Lalitha"]
    for d in week:
        if d.weekday() == 6:
            continue
        day_names = random.sample(names, len(names))
        for provider in ("dr-rao", "dr-lakshmi"):
            for hhmm in random.sample(["09:20", "10:00", "11:00", "11:40", "12:20", "17:00", "17:40", "18:20", "19:00"], 4):
                h, m = map(int, hhmm.split(":"))
                st = datetime.combine(d, time(h, m), tz)
                a = await db.add_appointment(patient_name=day_names.pop(), phone=random.choice(CALLERS),
                                             provider_id=provider, start=st, end=st + timedelta(minutes=20),
                                             call_id="seed" if random.random() < 0.7 else None)
                await db.update_appointment(a.id, fee_type=random.choice(["new", "new", "follow_up"]),
                                            fee_amount=random.choice([400, 450, 0]),
                                            visit="arrived" if d == today and h < 10 else None)
    for hhmm, name in (("12:40", "Sreelakshmi"), ("18:40", "Ta Marau")):
        h, m = map(int, hhmm.split(":"))
        st = datetime.combine(today, time(h, m), tz)
        a = await db.add_appointment(patient_name=name, phone="+919000016802", provider_id="dr-lakshmi", start=st,
                                     end=st + timedelta(minutes=20), call_id="seed")
        await db.update_appointment(a.id, confirmed=False, fee_type="new", fee_amount=450)
    meeting = datetime.combine(today + timedelta(days=1), time(12, 0), tz)
    await db.add_block(meeting, meeting + timedelta(hours=1), "dr-rao", "Hospital rounds")

    # today's OPD tokens
    opd = state.tools.opd
    for name, phone, age, reason, src in [("Ramesh", CALLERS[0], 52, "BP check", "call"),
                                           ("Sunitha", CALLERS[1], 34, "fever", "staff"),
                                           ("Mohammed Ali", CALLERS[2], 61, "sugar test", "call"),
                                           ("Kavya", CALLERS[3], 27, "headache", "staff"),
                                           ("Narayana", CALLERS[4], 70, "knee pain", "call")]:
        t, _ = await opd.issue(provider_id="dr-rao", day=today, patient_name=name, phone=phone, age=age,
                               reason=reason, source=src, session_hint="morning")
        if src == "staff" or name == "Ramesh":
            await opd.set_paid(t.id, True)
    await opd.call_next(today, "dr-rao", "09:00")
    await opd.call_next(today, "dr-rao", "09:00")
    for name, age, reason in [("Baby Diya", 1, "vaccination"), ("Rohan", 8, "cough")]:
        await opd.issue(provider_id="dr-lakshmi", day=today, patient_name=name, phone=CALLERS[5], age=age,
                        reason=reason, source="call", session_hint="morning")
    await opd.issue(provider_id="dr-lakshmi", day=today, patient_name="Vihaan", phone=CALLERS[6], age=4,
                    reason="ear pain", source="call", session_hint="morning", confirmed=False)

    # follow-ups and patient messages
    await state.tools.add_follow_up("callback", "Wants a call back about a home visit for her father (78).",
                                    call_id="seed-cb", phone=CALLERS[7], patient_name="Lalitha")
    for kind, ch, to, status, preview, err in [
        ("reminder", "whatsapp", CALLERS[0], "sent", "Reminder: your appointment with Dr. Srinivasa Rao is tomorrow at 5:20 PM.", None),
        ("token", "whatsapp", CALLERS[1], "sent", "Sunrise Family Clinic: OPD token 4 for Dr. Lakshmi Devi, morning OPD. Please come by 11:10 AM.", None),
        ("booking", "whatsapp", CALLERS[2], "failed", "Sunrise Family Clinic: appointment booked with Dr. Rao, Wed 5:00 PM.", "Recipient is not on WhatsApp"),
        ("booking", "sms", CALLERS[2], "sent", "Sunrise Family Clinic: appointment booked with Dr. Rao, Wed 5:00 PM.", None),
        ("booking", "email", "anjali.r@example.com", "sent", "Sunrise Family Clinic: appointment booked with Dr. Lakshmi Devi, Thu 6:20 PM.", None),
        ("manual", "whatsapp", CALLERS[3], "sent", "Sunrise Family Clinic: Dr. Rao is running about 20 minutes late today. Sorry for the wait.", None),
    ]:
        await db.log_message(kind, ch, to, status, preview, err)

    # spread "message sent" and booking times over the morning so the tables read naturally
    async with db.session() as s:
        await s.execute(update(Appointment).where(Appointment.call_id == "seed").values(call_id=None))
        await s.execute(update(Call).where(Call.ended_at.is_(None)).values(status="completed", outcome="info"))
        await s.commit()


# ------------------------------------------------------------------ browser
def _shrink(path: Path, width: int = 1600) -> None:
    """Keep the repo light: 1600 px wide, 256-colour PNG (still sharp for UI). Needs Pillow; skipped if absent."""
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(path)
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    im.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
        path, optimize=True)


async def shoot(state: AppState, today: date) -> None:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2,
                                        timezone_id="Asia/Kolkata", locale="en-IN")
        page = await ctx.new_page()

        async def snap(name: str, full: bool = False, wait: int = 900) -> None:
            await page.wait_for_timeout(wait)
            path = OUT / f"{name}.png"
            await page.screenshot(path=str(path), full_page=full)
            _shrink(path)
            print("  ", name)

        # public pages
        await page.goto(f"{BASE}/signup")
        await snap("signup")
        await page.goto(f"{BASE}/login")
        await snap("login")

        # chat demo: type three messages, the agent answers with cards
        await page.set_viewport_size({"width": 1440, "height": 1180})
        await page.goto(f"{BASE}/demo")
        await page.wait_for_timeout(800)
        for msg, expect, shot in [
                ("Book an appointment tomorrow evening", "Which doctor", None),
                ("Dr. Rao at 5:20 please. I'm Sai Charan, 90000 12345. Fever and body pain.", "is requested", "demo"),
                ("Also an OPD token today for my son Aarav, 6 years, cough", "token 4", "demo-token")]:
            await page.fill("#textInput", msg)
            await page.press("#textInput", "Enter")
            await page.wait_for_function(f"document.querySelector('#messages').innerText.includes({expect!r})",
                                         timeout=15000)
            await page.wait_for_timeout(700)
            if shot:
                await page.evaluate("document.getElementById('chatBody').scrollTop = 1e6")
                await snap(shot)
        await page.set_viewport_size({"width": 1440, "height": 900})

        # staff pages as the clinic owner
        await ctx.request.post(f"{BASE}/api/login", data={"email": OWNER[0], "password": OWNER[1]})
        await page.goto(f"{BASE}/dashboard")
        await snap("dashboard", wait=1500)
        await page.goto(f"{BASE}/dashboard")
        await page.wait_for_timeout(1200)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await snap("messages", wait=500)
        await page.click("#newMsg")
        await page.fill("#msgForm [name=phone]", "+91 90000 12345")
        await page.click("#msgForm .chip-sm")
        await snap("message-patient", wait=400)
        await page.keyboard.press("Escape")
        await page.goto(f"{BASE}/dashboard")
        await page.wait_for_timeout(1200)
        await page.evaluate(f"document.querySelector('#calls tr[data-id=\"{state.transcript_call_id}\"]')?.click()")
        await snap("call-transcript", wait=900)
        for path, name in [("/report", "report"), ("/opd", "opd"), ("/calendar", "calendar"),
                           ("/knowledge", "knowledge"), ("/setup", "setup")]:
            await page.goto(f"{BASE}{path}")
            await snap(name, wait=1500)

        # platform admin
        await ctx.request.post(f"{BASE}/api/logout")
        await ctx.request.post(f"{BASE}/api/login", data={"email": ADMIN[0], "password": ADMIN[1]})
        await page.goto(f"{BASE}/admin")
        await snap("admin", wait=1200)

        # phone-sized chat
        phone = await browser.new_context(**pw.devices["Pixel 7"], timezone_id="Asia/Kolkata")
        page = await phone.new_page()
        state.llm.script = demo_llm(today).script
        await page.goto(f"{BASE}/demo")
        await page.wait_for_timeout(800)
        await page.fill("#textInput", "Book an appointment tomorrow evening")
        await page.press("#textInput", "Enter")
        await page.wait_for_function("document.querySelector('#messages').innerText.includes('Which doctor')",
                                     timeout=15000)
        await snap("mobile", wait=800)
        await browser.close()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}",
                            business_config_path=str(ROOT_DIR / "config" / "business.yaml"),
                            openai_api_key="sk-demo-not-used", sarvam_api_key="demo", admin_user=ADMIN[0],
                            admin_password=ADMIN[1], secret_key="screenshots", embedding_model="off",
                            twilio_validate_signature=False, end_of_turn_delay_ms=10)
        biz_tz = Business.now  # keep the real clock function
        state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
        today = biz_tz(state.business).date()
        if today.weekday() == 6:  # the sample clinic is closed on Sunday: show Monday instead
            today += timedelta(days=1)
        frozen = datetime.combine(today, time(10, 30), state.business.tz)
        Business.now = lambda self: frozen  # every page shows the same moment: 10:30 on a working day
        state.llm = demo_llm(today)
        biz = state.business
        biz.pilot_mode = True
        biz.phone = "+91 40 2345 6789"
        biz.handoff_number = "+914023456789"
        state.notifier.channels = {"whatsapp": SentChannel(), "sms": SentChannel(), "email": SentChannel()}

        server = uvicorn.Server(uvicorn.Config(create_app(settings, state), port=PORT, log_level="warning"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.1)
        try:
            await seed(state, today)
            await shoot(state, today)
        finally:
            server.should_exit = True
            await task
    print(f"Screenshots written to {OUT.relative_to(ROOT_DIR)}/")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
