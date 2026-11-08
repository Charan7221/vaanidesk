"""Staff/clinic administration, call history on the dashboard, and what survives a server restart."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.background import drain
from app.main import create_app
from app.state import AppState, UnknownClinic
from app.tools.opd import OpdDesk
from app.tools.registry import ToolContext, ToolExecutor
from app.tools.twilio_client import TwilioClient

from . import test_opd
from .fakes import FakeLLM, FakeSTT, FakeTTS
from .test_messaging import FakeChannel, notifier
from .test_multiclinic import ADMIN, OWNER, login, new_clinic

AUTH = ("admin", "secret")
clock = test_opd.clock  # shared fixture: frozen clinic clock


def build(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    return state, TestClient(create_app(settings, state))


# ------------------------------------------------------------------ staff accounts
def test_owner_manages_staff_roles_and_passwords(settings):
    state, c = build(settings)
    with c:
        login(c, ADMIN)
        new_clinic(c)
        c.post("/api/logout")
        login(c, OWNER)
        assert c.post("/api/staff", json={"email": "desk@sai.clinic", "password": "desk-pass-123",
                                          "role": "boss"}).status_code == 422
        assert c.post("/api/staff", json={"email": "not-an-email", "password": "desk-pass-123"}).status_code == 422
        assert c.post("/api/staff", json={"email": "desk@sai.clinic", "password": "desk-pass-123"}).status_code == 200
        assert c.post("/api/staff", json={"email": "DESK@sai.clinic", "password": "desk-pass-123"}).status_code == 409
        assert c.post("/api/staff", json={"email": "admin", "password": "desk-pass-123"}).status_code == 422
        staff = {u["email"]: u for u in c.get("/api/staff").json()}
        me, desk = staff["owner@sai.clinic"]["id"], staff["desk@sai.clinic"]["id"]

        assert c.post(f"/api/staff/{me}", json={"role": "staff"}).status_code == 422  # can't demote yourself
        assert c.post("/api/staff/nope", json={"role": "staff"}).status_code == 404
        assert c.post(f"/api/staff/{desk}", json={"role": "superadmin"}).status_code == 422
        assert c.post(f"/api/staff/{desk}", json={"password": "123"}).status_code == 422
        assert c.post(f"/api/staff/{desk}", json={"role": "owner", "password": "new-desk-pass-9"}).json() == {"ok": True}
        audit = c.get("/api/audit").json()
        assert audit[0]["action"] == "staff.update" and "reset" in str(audit[0]["detail"])
        c.post("/api/logout")

        assert c.post("/api/login", json={"email": "desk@sai.clinic", "password": "desk-pass-123"}).status_code == 401
        login(c, {"email": "desk@sai.clinic", "password": "new-desk-pass-9"})
        assert c.get("/api/me").json()["role"] == "owner"  # promotion took effect
        assert c.get("/api/staff").status_code == 200


# ------------------------------------------------------------------ platform admin: clinics
def test_admin_updates_deactivates_and_reactivates_a_clinic(settings):
    state, c = build(settings)
    with c:
        login(c, ADMIN)
        new_clinic(c)
        assert c.post("/api/admin/clinics/nope", json={"active": False}).status_code == 404
        assert c.post("/api/admin/clinics/sai", json={"phone_numbers": ["12"]}).status_code == 422
        assert c.post("/api/admin/clinics/default", json={"active": False}).status_code == 422
        assert c.post("/api/admin/clinics/sai", json={"phone_numbers": ["+91 40 2345 0000"]}).json() == {"ok": True}
        assert c.portal.call(state.clinic_for_number, "+914023450000") == "sai"
        assert c.portal.call(state.clinic_for_number, "+914023456789") == "default"  # old number released

        assert c.post("/api/admin/switch/nope").status_code == 404
        assert c.post("/api/admin/switch/sai").json() == {"clinic_id": "sai"}
        assert c.get("/api/me").json()["clinic_id"] == "sai"

        assert c.post("/api/admin/clinics/sai", json={"active": False}).json() == {"ok": True}
        assert "sai" not in state.tenants
        assert c.portal.call(state.clinic_for_number, "+914023450000") == "default"  # calls go to the default clinic
        assert c.post("/api/admin/clinics/sai", json={"active": True}).json() == {"ok": True}
        assert state.tenant("sai").business.name == "Sai Clinic"


# ------------------------------------------------------------------ restart
def test_clinics_and_their_settings_survive_a_restart(settings):
    state, c = build(settings)
    with c:
        login(c, ADMIN)
        new_clinic(c)
        dup = c.post("/api/admin/clinics", json={"id": "gone", "name": "Dup", "template": "clinic",
                                                 "owner_email": OWNER["email"], "owner_password": OWNER["password"]})
        assert dup.status_code == 409  # one email, one account; nothing half-created
    assert "gone" not in state.tenants
    with c:
        login(c, ADMIN)
        c.post("/api/admin/switch/sai")
        cfg = c.get("/api/setup").json()["config"]
        cfg["name"], cfg["notifications"]["phone_channels"] = "Sai Clinic Kukatpally", ["sms"]
        assert c.put("/api/setup", json={"config": cfg}).json() == {"saved": True}

    state2, c2 = build(settings)  # new process, same database
    assert "sai" not in state2.tenants
    with c2:
        biz = state2.tenant("sai").business
        assert biz.name == "Sai Clinic Kukatpally" and biz.notifications.phone_channels == ["sms"]
        assert state2.tenant("default").business.name == state.tenant("default").business.name
        login(c2, OWNER)  # the owner's account is in the database too
        assert c2.get("/api/me").json()["clinic_id"] == "sai"
        login(c2, ADMIN)
        c2.post("/api/admin/clinics/sai", json={"active": False})

    state3, c3 = build(settings)
    with c3:
        assert "sai" not in state3.tenants  # deactivated clinics stay off after a restart
        try:
            c3.portal.call(state3.save_clinic_config, "missing", state3.business)
            raise AssertionError("expected UnknownClinic")
        except UnknownClinic:
            pass


# ------------------------------------------------------------------ call history
def test_call_history_detail_and_appointment_list(settings):
    state, c = build(settings)
    with c:
        db = state.db
        call = c.portal.call(db.create_call, "phone", "+919346968532", "CA1")
        c.portal.call(db.add_turn, call.id, "user", "Dr. Rao appointment kavali", "te-IN", None)
        c.portal.call(db.add_turn, call.id, "assistant", "Tappakunda", "te-IN", 820.4)
        c.portal.call(lambda: db.finish_call(call.id, status="completed", outcome="booked", language="te-IN"))

        calls = c.get("/api/calls?days=1", auth=AUTH).json()
        assert [x["id"] for x in calls] == [call.id] and calls[0]["outcome"] == "booked"
        assert c.get("/api/calls?days=1&limit=1000", auth=AUTH).status_code == 200
        detail = c.get(f"/api/calls/{call.id}", auth=AUTH).json()
        assert [t["role"] for t in detail["turns"]] == ["user", "assistant"]
        assert detail["turns"][1]["latency_ms"] == 820 and detail["turns"][0]["language"] == "te-IN"
        assert c.get("/api/calls/nope", auth=AUTH).status_code == 404

        from .conftest import next_weekday
        day = next_weekday(state.business, 0)
        ctx = ToolContext(call_id=call.id, channel="phone", caller_phone="+919346968532")
        r = c.portal.call(state.tools.run, ctx, "book_appointment", {
            "patient_name": "Sai Charan", "date": day.isoformat(), "time": "17:20", "provider_id": "dr-rao",
            "email": "sai@example.com"})
        assert r["booked"], r
        appts = c.get("/api/appointments", auth=AUTH).json()
        assert appts[0]["email"] == "sai@example.com" and appts[0]["provider"] == "Dr. Srinivasa Rao"
        assert c.post(f"/api/appointments/{r['appointment_id']}/cancel", auth=AUTH).json() == {"cancelled": True}
        assert c.post("/api/appointments/nope/cancel", auth=AUTH).status_code == 404
        assert c.get("/api/appointments?upcoming=false", auth=AUTH).json()[0]["status"] == "cancelled"


# ------------------------------------------------------------------ OPD desk edge cases
async def test_only_one_patient_with_the_doctor_and_rejecting_pending_tokens(db, business, monday, clock):
    opd = OpdDesk(db, business)
    a, _ = await opd.issue(provider_id="dr-rao", day=monday, patient_name="A", phone="+919000000001")
    b, _ = await opd.issue(provider_id="dr-rao", day=monday, patient_name="B", phone="+919000000002")
    await opd.set_token_status(a.id, "in_consultation")
    await opd.set_token_status(b.id, "in_consultation")  # calling B in finishes A
    assert (await opd.get(a.id)).status == "done" and (await opd.get(b.id)).status == "in_consultation"
    try:
        await opd.set_token_status(a.id, "teleported")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert await opd.set_token_status("missing", "done") is None
    assert (await opd.set_paid(b.id, True)).paid is True

    c, _ = await opd.issue(provider_id="dr-rao", day=monday, patient_name="C", phone="+919000000003", confirmed=False)
    assert (await opd.get(c.id)).confirmed is False  # shown as "Not confirmed" on the OPD queue
    rejected = await opd.set_confirmed(c.id, False)
    assert rejected.confirmed is False and rejected.status == "cancelled"
    assert await opd.set_confirmed("missing", True) is None


async def test_token_with_email_and_staff_alert_for_follow_ups(settings, db, business, calendar, monday, clock):
    n = notifier(settings, db)
    ex = ToolExecutor(business, calendar, TwilioClient(settings), notifier=n)
    ctx = ToolContext(call_id="c1", channel="phone", caller_phone="+919346968532")
    bad = await ex.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Ravi Kumar", "email": "x@"})
    assert bad["issued"] is False and "email" in bad["reason"]
    t = await ex.run(ctx, "get_opd_token", {"provider_id": "dr-rao", "patient_name": "Ravi Kumar",
                                            "email": "ravi@example.com", "age": "forty"})
    assert t["issued"] is True and t["confirmation_channels"] == ["whatsapp", "email"], t
    token = await ex.opd.get(t["token_id"])
    assert token.email == "ravi@example.com"
    assert (await ex.opd.patient(token.patient_id)).email == "ravi@example.com"

    assert (await ex.run(ctx, "request_callback", {"reason": "wants a home visit"}))["requested"] is True
    await drain()
    alerts = [to for to, text in n.channels["whatsapp"].sent if "follow-up" in text]
    assert alerts == [business.handoff_number]

    # confirmations switched off: nothing is promised to the caller and nothing is sent
    business.notifications.confirmations = False
    n.channels["whatsapp"] = FakeChannel()
    t2 = await ex.run(ctx, "get_opd_token", {"provider_id": "dr-lakshmi", "patient_name": "Sita Devi"})
    await drain()
    assert t2["issued"] and t2["confirmation_channels"] == [] and t2["confirmation_sms"] is False
    assert n.channels["whatsapp"].sent == []


# ------------------------------------------------------------------ web demo connection guards
def test_browser_call_guards_and_caller_details(settings):
    settings.max_concurrent_calls = 1
    state, c = build(settings)
    with c:
        with c.websocket_connect("/ws/browser") as ws:
            ws.send_json({"type": "start", "clinic": "nope"})
            assert ws.receive_json() == {"type": "error", "message": "Unknown clinic"}

        with c.websocket_connect("/ws/browser") as ws:
            ws.send_json({"type": "start", "phone": "93469 68532", "email": " Sai@Example.com ", "language": "xx"})
            assert ws.receive_json()["type"] == "ready"
            session = next(iter(state.active_sessions.values()))
            assert session.ctx.caller_phone == "+919346968532" and session.ctx.caller_email == "sai@example.com"
            assert session.ctx.language == state.business.languages[0]  # unknown language falls back

            with c.websocket_connect("/ws/browser") as second:  # only one line in this test
                second.send_json({"type": "start"})
                assert second.receive_json() == {"type": "error", "message": "All lines busy, try again shortly"}
            ws.send_json({"type": "hangup"})


def test_demo_page_follows_the_logged_in_clinic_and_setup_warnings(settings):
    state, c = build(settings)
    with c:
        assert c.get("/api/public-config").json()["clinic"] == "default"  # visitors: the first clinic
        login(c, ADMIN)
        new_clinic(c)
        c.post("/api/logout")
        login(c, OWNER)
        cfg = c.get("/api/public-config").json()
        assert cfg["clinic"] == "sai" and cfg["business"] == "Sai Clinic"  # staff test their own receptionist
        assert c.get("/api/public-config?clinic=default").json()["clinic"] == "default"  # explicit link wins
        warnings = c.get("/api/attention").json()["setup_warnings"]
        assert any("example number" in w for w in warnings) and any("WhatsApp or SMS" in w for w in warnings)
        cfg = c.get("/api/setup").json()["config"]
        cfg["handoff_number"] = "+914023456700"
        c.put("/api/setup", json={"config": cfg})
        assert not any("example number" in w for w in c.get("/api/attention").json()["setup_warnings"])
