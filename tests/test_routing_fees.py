"""Departments: problem -> doctor routing, consultation fees and free follow-ups, and the DB auto-migration."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.business import Business, load_business
from app.config import ROOT_DIR
from app.db import Database
from app.main import create_app
from app.pipeline.prompts import system_prompt
from app.state import AppState
from app.tools.opd import OpdDesk
from app.tools.routing import recommend

from .conftest import next_weekday
from .fakes import FakeLLM, FakeSTT, FakeTTS

HOSPITAL = load_business(str(ROOT_DIR / "config" / "hospital.yaml"))


# ------------------------------------------------------------------ routing
@pytest.mark.parametrize("problem,age,gender,dept", [
    ("high sugar and tiredness", 52, None, "general-medicine"),
    ("my son has fever", None, None, "paediatrics"),
    ("fever", 6, None, "paediatrics"),          # a child goes to paediatrics even for adult words
    ("fever", 40, None, "general-medicine"),
    ("something odd", None, None, "general-medicine"),  # no match -> default department
])
def test_clinic_routing(business, problem, age, gender, dept):
    r = recommend(business, problem, age, gender)
    assert r["department_id"] == dept
    assert r["doctors"] and "possible_emergency" not in r


@pytest.mark.parametrize("problem,age,gender,dept", [
    ("knee pain after a fall", 60, None, "orthopaedics"),
    ("irregular periods", 24, "female", "obstetrics-gynaecology"),
    ("irregular periods", None, "male", "general-medicine"),   # gender rule
    ("hair fall and dandruff", None, None, "dermatology"),
    ("palpitations", 50, None, "cardiology"),
    ("blurred vision", None, None, "ophthalmology"),
    ("ear pain", 30, None, "ent"),
    ("baby not feeding well", None, None, "paediatrics"),
])
def test_hospital_routing(problem, age, gender, dept):
    assert recommend(HOSPITAL, problem, age, gender)["department_id"] == dept


def test_routing_flags_emergencies_and_fees():
    r = recommend(HOSPITAL, "severe chest pain since 10 minutes", 58)
    assert r["possible_emergency"] == ["chest pain"] and "emergency=true" in r["instruction"]
    cardio = recommend(HOSPITAL, "palpitations", 50)
    assert cardio["doctors"][0]["consultation_fee"] == "₹1000"  # doctor's own fee overrides the department's
    assert recommend(HOSPITAL, "eye pain")["doctors"][0]["consultation_fee"] == "₹400"
    assert recommend(HOSPITAL, "knee pain")["needs_age"] is True


async def test_find_doctor_tool(executor, ctx):
    r = await executor.run(ctx, "find_doctor", {"problem": "my daughter has a cough", "age": "7"})
    assert r["department_id"] == "paediatrics" and r["doctors"][0]["provider_id"] == "dr-lakshmi"
    assert r["doctors"][0]["consultation_fee"] == "₹450" and "free" in r["follow_up"]


def test_prompt_lists_departments_and_fees(business):
    p = system_prompt(business)
    assert "General Medicine, fee ₹400" in p and "Paediatrics" in p and "find_doctor" in p
    assert "within 7 days is a follow-up: free" in p
    hosp = system_prompt(HOSPITAL)
    assert "Dr. Anand Varma (Cardiologist, Cardiology, fee ₹1000)" in hosp


# ------------------------------------------------------------------ fees and follow-ups
@pytest.fixture
def clock(business, monday, monkeypatch):
    state = {"now": datetime(monday.year, monday.month, monday.day, 8, 0, tzinfo=business.tz)}
    monkeypatch.setattr(Business, "now", lambda self: state["now"])

    def set_day(day):
        state["now"] = datetime(day.year, day.month, day.day, 8, 0, tzinfo=business.tz)

    return set_day


async def test_first_visit_paid_then_free_follow_up(db, business, monday, clock):
    opd = OpdDesk(db, business)
    first, _ = await opd.issue(provider_id="dr-rao", day=monday, patient_name="Ravi", phone="+919000000001")
    assert (first.fee_type, first.fee_amount) == ("new", 400)

    tuesday = monday + timedelta(days=1)
    clock(tuesday)
    again, _ = await opd.issue(provider_id="dr-rao", day=tuesday, patient_name="Ravi", phone="+919000000001")
    assert (again.fee_type, again.fee_amount) == ("follow_up", 0)
    pos = await opd.position(again)
    assert pos["fee"] == "free follow-up" and pos["fee_paid"] is False

    # a family member on the same phone is a different patient: pays
    son, _ = await opd.issue(provider_id="dr-lakshmi", day=tuesday, patient_name="Aarav", phone="+919000000001")
    assert (son.fee_type, son.fee_amount) == ("new", 450)

    # after the 7-day window it's a new paid consultation again
    later = monday + timedelta(days=8)
    clock(later)
    third, _ = await opd.issue(provider_id="dr-rao", day=later, patient_name="Ravi", phone="+919000000001")
    assert (third.fee_type, third.fee_amount) == ("new", 400)

    # a cancelled first visit doesn't count
    await opd.set_token_status(third.id, "cancelled")
    quote = await opd.fee_quote(third.patient_id, "dr-rao", later)
    assert quote["type"] == "new"


async def test_appointment_then_token_is_follow_up(executor, ctx, monday, clock):
    booked = await executor.run(ctx, "book_appointment", {"patient_name": "Sita", "date": monday.isoformat(),
                                                          "time": "09:00", "provider_id": "dr-rao"})
    assert booked["fee"] == "₹400 consultation fee"
    clock(monday + timedelta(days=1))
    token = await executor.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Sita"})
    assert token["issued"] is True and token["fee"] == "free follow-up"

    # rescheduling keeps the original fee
    moved = await executor.run(ctx, "reschedule_appointment", {"appointment_id": booked["appointment_id"],
                                                               "date": (monday + timedelta(days=2)).isoformat(),
                                                               "time": "09:20"})
    appt = await executor.cal.db.get_appointment(moved["appointment_id"])
    assert (appt.fee_type, appt.fee_amount) == ("new", 400) and appt.patient_id


def test_reception_marks_paid_and_calendar_shows_fee(settings):
    app_state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    day = next_weekday(app_state.business, 0)
    original = Business.now
    Business.now = lambda self: datetime(day.year, day.month, day.day, 8, 0, tzinfo=self.tz)
    auth = ("admin", "secret")
    try:
        with TestClient(create_app(settings, app_state)) as c:
            r = c.post("/api/opd/tokens", auth=auth, json={"provider_id": "dr-rao", "patient_name": "Anil",
                                                            "session": "09:00", "phone": "9000000005"}).json()
            assert (r["fee_type"], r["fee_amount"]) == ("new", 400)
            assert c.post(f"/api/opd/tokens/{r['id']}/paid", auth=auth, json={"paid": True}).json()["paid"] is True
            board = c.get("/api/opd", auth=auth).json()
            tok = board["doctors"][0]["sessions"][0]["tokens"][0]
            assert tok["paid"] is True and tok["fee_amount"] == 400 and board["currency"] == "₹"
            assert board["doctors"][0]["department"] == "General Medicine"
            assert c.post("/api/opd/tokens/nope/paid", auth=auth, json={"paid": True}).status_code == 404

            appt = c.post("/api/appointments", auth=auth, json={"date": day.isoformat(), "time": "18:00",
                                                                 "provider_id": "dr-rao", "patient_name": "Anil",
                                                                 "phone": "9000000005"}).json()
            assert appt["fee"].startswith("free follow-up")  # same patient, same department, same day
            week = c.get(f"/api/calendar?start={day.isoformat()}", auth=auth).json()
            assert week["appointments"][0]["fee"] == "Free follow-up"
    finally:
        Business.now = original


# ------------------------------------------------------------------ upgrade of an existing database
async def test_old_database_gets_new_columns(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    old = Database(url)
    async with old.engine.begin() as conn:  # an appointments table from an older VaaniDesk version
        await conn.execute(text("CREATE TABLE appointments (id VARCHAR(32) PRIMARY KEY, call_id VARCHAR(32), "
                                "patient_name VARCHAR(120), phone VARCHAR(32), provider_id VARCHAR(64), "
                                "start DATETIME, \"end\" DATETIME, reason VARCHAR(255), status VARCHAR(16), "
                                "external_event_id VARCHAR(255), created_at DATETIME)"))
    await old.dispose()
    db = Database(url)
    await db.create_all()
    async with db.engine.begin() as conn:
        cols = {r[1] for r in (await conn.execute(text("PRAGMA table_info(appointments)"))).all()}
    assert {"patient_id", "fee_type", "fee_amount"} <= cols
    await db.dispose()
