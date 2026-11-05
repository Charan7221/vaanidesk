"""Many clinics on one server: data isolation, logins and roles, setup screen, clinic admin, call routing."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.business import Business, load_business
from app.config import ROOT_DIR
from app.db import current_clinic
from app.main import create_app
from app.state import AppState
from app.tools.opd import OpdDesk
from app.tools.registry import ToolContext

from .conftest import next_weekday
from .fakes import FakeLLM, FakeSTT, FakeTTS

ADMIN = {"email": "admin", "password": "secret"}
OWNER = {"email": "owner@sai.clinic", "password": "owner-pass-123"}


@pytest.fixture
def app_state(settings):
    return AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())


@pytest.fixture
def client(settings, app_state):
    with TestClient(create_app(settings, app_state)) as c:
        yield c


def login(c, creds) -> TestClient:
    r = c.post("/api/login", json=creds)
    assert r.status_code == 200, r.text
    return c


def new_clinic(c, cid="sai", template="clinic", numbers=("+914023456789",)):
    r = c.post("/api/admin/clinics", json={"id": cid, "name": "Sai Clinic", "template": template,
                                           "owner_email": OWNER["email"], "owner_password": OWNER["password"],
                                           "phone_numbers": list(numbers)})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ isolation at the data layer
async def test_clinics_never_see_each_others_data(db, business, monday, at):
    from app.tools.calendar import LocalCalendar
    cal = LocalCalendar(db, business)
    token = current_clinic.set("clinic-a")
    try:
        await cal.book(patient_name="Asha", start=at(monday, "09:00"), provider_id="dr-rao", phone="+919000000001")
        await db.add_follow_up("callback", "A's request", phone="+919000000001")
        pa = await OpdDesk(db, business).register_patient("Asha", "+919000000001")
    finally:
        current_clinic.reset(token)
    token = current_clinic.set("clinic-b")
    try:
        assert await db.find_appointments(phone="+919000000001") == []
        assert await db.follow_ups() == []
        assert await db.get_appointment("nope") is None
        pb = await OpdDesk(db, business).register_patient("Asha", "+919000000001")  # separate record per clinic
        assert pb.id != pa.id and pb.number != pa.number  # patient numbers stay unique on the server
        # the same slot in another clinic is a different calendar
        assert at(monday, "09:00") in await cal.available_slots(monday, "dr-rao")
    finally:
        current_clinic.reset(token)
    assert len(await db.find_appointments(phone="+919000000001")) == 0  # default clinic: nothing either


# ------------------------------------------------------------------ logins and roles
def test_login_roles_and_clinic_setup(client, app_state):
    c = client
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"email": "admin", "password": "wrong"}).status_code == 401
    page = c.get("/dashboard", headers={"Accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200

    login(c, ADMIN)
    assert c.get("/api/me").json()["role"] == "superadmin"
    new_clinic(c)
    assert c.post("/api/admin/clinics", json={"id": "sai", "name": "Other", "owner_email": "x@y.z",
                                               "owner_password": "long-enough-1"}).status_code == 409
    clinics = {x["id"]: x for x in c.get("/api/admin/clinics").json()}
    assert clinics["sai"]["phone_numbers"] == ["+914023456789"] and clinics["sai"]["staff"] == 1
    c.post("/api/logout")

    # the owner sees only their clinic, with the template's doctors renamed to unique ids
    login(c, OWNER)
    me = c.get("/api/me").json()
    assert me == {**me, "role": "owner", "clinic_id": "sai", "clinic_name": "Sai Clinic"}
    assert c.get("/api/admin/clinics").status_code == 403
    setup = c.get("/api/setup").json()
    assert [p["id"] for p in setup["config"]["providers"]] == ["sai-dr-rao", "sai-dr-lakshmi"]

    # owner edits the profile: new doctor, new hours; validation catches mistakes
    cfg = setup["config"]
    cfg["providers"].append({"id": "sai-dr-kumar", "name": "Dr. Kumar", "specialty": "ENT", "department": None})
    cfg["hours"]["sun"] = [["10:00", "12:00"]]
    assert c.put("/api/setup", json={"config": cfg}).json() == {"saved": True}
    assert app_state.tenant("sai").business.provider("sai-dr-kumar").name == "Dr. Kumar"
    bad = {**cfg, "providers": cfg["providers"] + [{"id": "dr-rao", "name": "Copy", "specialty": ""}]}
    assert "another clinic" in c.put("/api/setup", json={"config": bad}).json()["detail"]
    bad = {**cfg, "hours": {**cfg["hours"], "mon": [["13:00", "09:00"]]}}
    assert c.put("/api/setup", json={"config": bad}).status_code == 422

    # owner adds a receptionist; weak passwords are refused
    assert c.post("/api/staff", json={"email": "desk@sai.clinic", "password": "short"}).status_code == 422
    assert c.post("/api/staff", json={"email": "desk@sai.clinic", "password": "desk-pass-123"}).status_code == 200
    staff = c.get("/api/staff").json()
    assert {u["email"] for u in staff} == {"owner@sai.clinic", "desk@sai.clinic"}
    me_id = next(u["id"] for u in staff if u["role"] == "owner")
    assert c.post(f"/api/staff/{me_id}", json={"active": False}).status_code == 422  # can't lock yourself out
    actions = [a["action"] for a in c.get("/api/audit").json()]
    assert {"login", "setup.save", "staff.add"} <= set(actions)
    c.post("/api/logout")

    # the receptionist can run the desk but not change setup or staff
    login(c, {"email": "desk@sai.clinic", "password": "desk-pass-123"})
    assert c.get("/api/stats").status_code == 200
    assert c.get("/api/opd").json()["doctors"][0]["id"] == "sai-dr-rao"
    assert c.get("/api/setup").status_code == 403 and c.get("/api/staff").status_code == 403
    assert c.post("/api/knowledge", files={"file": ("x.md", b"# X\nhello")}).status_code == 403
    c.post("/api/logout")

    # a disabled account can't log in
    login(c, OWNER)
    desk = next(u["id"] for u in c.get("/api/staff").json() if u["role"] == "staff")
    c.post(f"/api/staff/{desk}", json={"active": False})
    c.post("/api/logout")
    assert c.post("/api/login", json={"email": "desk@sai.clinic", "password": "desk-pass-123"}).status_code == 401


def test_staff_only_see_their_clinics_bookings(client, app_state):
    c = login(client, ADMIN)
    new_clinic(c)
    day = next_weekday(app_state.business, 0)
    # a booking in the first clinic (as the platform admin, default clinic)
    r = c.post("/api/appointments", json={"date": day.isoformat(), "time": "09:00", "provider_id": "dr-rao",
                                          "patient_name": "Default Patient"})
    assert r.status_code == 200
    c.post("/api/logout")
    login(c, OWNER)
    assert c.get("/api/appointments").json() == []
    ok = c.post("/api/appointments", json={"date": day.isoformat(), "time": "09:00", "provider_id": "sai-dr-rao",
                                           "patient_name": "Sai Patient"})
    assert ok.status_code == 200
    assert [a["patient_name"] for a in c.get("/api/appointments").json()] == ["Sai Patient"]
    # superadmin can switch to the clinic and see the same
    c.post("/api/logout")
    login(c, ADMIN)
    c.post("/api/admin/switch/sai")
    assert [a["patient_name"] for a in c.get("/api/appointments").json()] == ["Sai Patient"]
    assert c.get("/api/me").json()["clinic_id"] == "sai"


# ------------------------------------------------------------------ calls reach the right clinic
def test_phone_number_and_demo_link_pick_the_clinic(client, app_state):
    c = login(client, ADMIN)
    new_clinic(c, template="hospital")
    twiml = c.post("/twilio/voice", data={"From": "+919876543210", "To": "+914023456789", "CallSid": "CA1"}).text
    assert 'name="clinic" value="sai"' in twiml
    other = c.post("/twilio/voice", data={"From": "+919876543210", "To": "+15550000000", "CallSid": "CA2"}).text
    assert 'name="clinic" value="default"' in other
    assert c.get("/api/public-config?clinic=sai").json()["business"] == "Sai Clinic"
    assert c.get("/api/public-config?clinic=nope").status_code == 404

    with c.websocket_connect("/ws/browser") as ws:
        ws.send_json({"type": "start", "clinic": "sai", "language": "en-IN"})
        assert ws.receive_json()["type"] == "ready"
        session = next(iter(app_state.active_sessions.values()))
        assert session.d.business.name == "Sai Clinic"
        assert any(p.department == "orthopaedics" for p in session.d.business.providers)
        ws.send_json({"type": "hangup"})
    with c.websocket_connect("/ws/browser") as ws:
        ws.send_json({"type": "start", "clinic": "nope"})
        assert ws.receive_json() == {"type": "error", "message": "Unknown clinic"}


def test_each_clinic_has_its_own_calendar_feed(client, app_state):
    from app.api.calendar import feed_token
    c = login(client, ADMIN)
    new_clinic(c)
    s = app_state.settings
    assert feed_token(s, "sai") != feed_token(s, "default")
    assert client.get(f"/calendar/{feed_token(s, 'sai')}.ics").text.startswith("BEGIN:VCALENDAR")
    assert "Sai Clinic" in client.get(f"/calendar/{feed_token(s, 'sai')}.ics").text
    assert client.get("/calendar/wrong.ics").status_code == 404


async def test_tool_calls_run_inside_the_callers_clinic(app_state, monday):
    await app_state.db.create_all()
    biz = load_business(str(ROOT_DIR / "config" / "hospital.yaml"))
    for p in biz.providers:
        p.id = "h-" + p.id
    app_state.add_tenant("hosp", biz)
    token = current_clinic.set("hosp")
    original = Business.now
    Business.now = lambda self: datetime(monday.year, monday.month, monday.day, 8, 0, tzinfo=self.tz)
    try:
        ctx = ToolContext(call_id="c1", channel="phone", caller_phone="+919346968532")
        r = await app_state.tools.run(ctx, "find_doctor", {"problem": "knee pain", "age": 50})
        assert r["doctors"][0]["provider_id"] == "h-dr-ravi"
        t = await app_state.tools.run(ctx, "get_opd_token", {"provider_id": "h-dr-ravi", "patient_name": "Ravi"})
        assert t["issued"] and t["fee"] == "₹600"
    finally:
        Business.now = original
        current_clinic.reset(token)
    assert (await app_state.db.find_appointments()) == []
