"""Self-service sign-up for clinics, and protection against password guessing."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.state import AppState

from .fakes import FakeLLM, FakeSTT, FakeTTS

NEW = {"clinic_name": "Sai Family Clinic", "your_name": "Dr. Sai", "email": "sai@clinic.in",
       "password": "a-long-password", "template": "clinic", "phone": "9346968532"}


def make(settings, **over):
    s = settings.model_copy(update=over)
    state = AppState.build(s, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    return TestClient(create_app(s, state)), state


def test_clinic_signs_up_and_lands_in_its_own_clinic(settings):
    c, state = make(settings)
    with c:
        assert c.get("/signup").status_code == 200
        assert c.get("/api/signup-config").json() == {"enabled": True, "code_required": False}
        r = c.post("/api/signup", json=NEW)
        assert r.status_code == 200 and r.json()["clinic_id"] == "sai-family-clinic"
        me = c.get("/api/me").json()  # logged in straight away, as the owner of the new clinic
        assert me["role"] == "owner" and me["clinic_name"] == "Sai Family Clinic" and me["pilot_mode"] is True
        biz = state.tenant("sai-family-clinic").business
        assert biz.phone == "+919346968532" and biz.providers[0].id == "sai-family-clinic-dr-rao"
        assert c.get("/api/public-config?clinic=sai-family-clinic").json()["emergency_number"] == "108"
        c.post("/api/logout")

        # same name again -> its own id; same email -> refused
        again = c.post("/api/signup", json={**NEW, "email": "other@clinic.in"})
        assert again.json()["clinic_id"] == "sai-family-clinic-2"
        c.post("/api/logout")
        assert c.post("/api/signup", json={**NEW, "clinic_name": "X Clinic"}).status_code == 409
        assert c.post("/api/signup", json={**NEW, "email": "z@clinic.in", "password": "short"}).status_code == 422
        assert c.post("/api/signup", json={**NEW, "email": "admin"}).status_code == 422


def test_signup_can_be_closed_or_invite_only(settings):
    c, _ = make(settings, signup_enabled=False)
    with c:
        assert c.post("/api/signup", json=NEW).status_code == 403
    c, _ = make(settings, signup_code="VD2026")
    with c:
        assert c.get("/api/signup-config").json()["code_required"] is True
        assert c.post("/api/signup", json=NEW).status_code == 403
        assert c.post("/api/signup", json={**NEW, "code": "VD2026"}).status_code == 200


def test_password_guessing_is_slowed_down(settings):
    c, _ = make(settings)
    with c:
        for _ in range(8):
            assert c.post("/api/login", json={"email": "admin", "password": "guess"}).status_code == 401
        blocked = c.post("/api/login", json={"email": "admin", "password": "secret"})
        assert blocked.status_code == 429  # even the right password waits after 8 wrong ones
        assert c.post("/api/login", json={"email": "someone@else.in", "password": "x"}).status_code == 401


def test_signup_spam_is_limited(settings):
    c, _ = make(settings)
    with c:
        for i in range(5):
            assert c.post("/api/signup", json={**NEW, "email": f"u{i}@clinic.in"}).status_code == 200
            c.post("/api/logout")
        assert c.post("/api/signup", json={**NEW, "email": "u9@clinic.in"}).status_code == 429
