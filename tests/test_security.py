"""Security hardening: production refuses unsafe settings, browser protections, revocable logins, chat abuse
limits, private logs. (Caller identity / one-time codes are tested in test_tools.py and test_opd.py.)"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.phone import mask_phone
from app.security import client_ip
from app.state import AppState

from .fakes import FakeLLM, FakeSTT, FakeTTS, tool
from .test_multiclinic import ADMIN, OWNER, login, new_clinic

STRONG = {"secret_key": "k" * 48, "admin_password": "a-long-admin-password-2026"}


def prod(settings: Settings, **over) -> Settings:
    return settings.model_copy(update={"app_env": "production", **STRONG, **over})


def next_event(ws) -> dict:
    """Next JSON event from the server, skipping audio frames."""
    import json

    while True:
        msg = ws.receive()
        if msg.get("text"):
            return json.loads(msg["text"])


def build(settings, llm=None):
    state = AppState.build(settings, stt=FakeSTT(), llm=llm or FakeLLM(), tts=FakeTTS())
    return state, TestClient(create_app(settings, state))


# ------------------------------------------------------------------ production settings
@pytest.mark.parametrize("bad, needle", [
    ({"secret_key": ""}, "SECRET_KEY"),
    ({"secret_key": "short"}, "SECRET_KEY"),
    ({"admin_password": "change-me"}, "ADMIN_PASSWORD"),
    ({"admin_password": "short-pw"}, "ADMIN_PASSWORD"),
])
def test_production_refuses_unsafe_settings(settings, bad, needle):
    with pytest.raises(RuntimeError, match=needle):
        create_app(prod(settings, **bad))


def test_render_counts_as_production(settings, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    assert settings.is_production
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(settings)


def test_production_hides_api_docs_and_error_details(settings):
    s = prod(settings)
    assert s.production_problems() == []
    state, c = build(s)
    with c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
        assert state.new_session  # sessions in production don't send provider errors to the browser
    _, dev = build(settings)
    with dev:
        assert dev.get("/docs").status_code == 200  # still there for development


def test_exotel_stream_closed_in_production_without_a_secret(settings):
    _, c = build(prod(settings, exotel_stream_secret=""))
    with c, pytest.raises(WebSocketDisconnect), c.websocket_connect("/exotel/stream") as ws:
        ws.receive_text()


# ------------------------------------------------------------------ browser protections
def test_security_headers(settings):
    _, c = build(settings)
    with c:
        page = c.get("/demo")
        h = page.headers
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        csp = h["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
        assert "microphone=(self)" in h["permissions-policy"]
        assert "<script>" not in page.text  # no inline scripts, so the strict policy doesn't break pages
        for path in ("/login", "/signup"):
            assert "<script>" not in c.get(path).text
        assert c.get("/api/public-config").headers["cache-control"] == "no-store"
        hsts = c.get("/demo", headers={"x-forwarded-proto": "https"}).headers
        assert "max-age" in hsts["strict-transport-security"]


def test_client_ip_ignores_addresses_the_client_made_up():
    class Client:
        host = "10.0.0.5"

    assert client_ip({"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, Client) == "203.0.113.9"  # proxy-appended
    assert client_ip({}, Client) == "10.0.0.5"


# ------------------------------------------------------------------ logins can be revoked
def test_changing_a_password_logs_out_old_sessions(settings):
    state, c = build(settings)
    with c:
        login(c, ADMIN)
        new_clinic(c)
        c.post("/api/logout")
        login(c, OWNER)
        me = next(u for u in c.get("/api/staff").json() if u["email"] == OWNER["email"])
        old_cookie = c.cookies.get("vd_session")
        assert c.post(f"/api/staff/{me['id']}", json={"password": "a-brand-new-pass-9"}).json() == {"ok": True}
        c.cookies.clear()
        c.cookies.set("vd_session", old_cookie)
        assert c.get("/api/me").status_code == 401  # a stolen/old cookie stops working
        login(c, {"email": OWNER["email"], "password": "a-brand-new-pass-9"})
        assert c.get("/api/me").status_code == 200


def test_changing_the_admin_password_logs_out_admin_sessions(settings):
    state, c = build(settings)
    with c:
        login(c, ADMIN)
        assert c.get("/api/me").status_code == 200
        state.settings.admin_password = "rotated-admin-password-1"
        assert c.get("/api/me").status_code == 401


def test_tampered_cookie_is_rejected(settings):
    _, c = build(settings)
    with c:
        login(c, ADMIN)
        token = c.cookies.get("vd_session")
        payload, sig = token.rsplit(".", 1)
        c.cookies.clear()
        c.cookies.set("vd_session", payload + "." + ("0" if sig[0] != "0" else "1") + sig[1:])
        assert c.get("/api/me").status_code == 401


# ------------------------------------------------------------------ public chat abuse limits
def test_chat_limits_per_visitor(settings):
    s = settings.model_copy(update={"chat_concurrent_per_ip": 1, "chat_sessions_per_hour": 2, "chat_max_chars": 50,
                                    "chat_max_messages": 2})
    _, c = build(s)
    with c:
        with c.websocket_connect("/ws/browser") as ws:
            ws.send_json({"type": "start"})
            assert ws.receive_json()["type"] == "ready"
            with c.websocket_connect("/ws/browser") as second:  # one open conversation per visitor here
                second.send_json({"type": "start"})
                assert "already have a conversation" in second.receive_json()["message"]
            ws.send_json({"type": "text", "text": "x" * 51})
            event = next_event(ws)
            while event.get("type") != "error":
                event = next_event(ws)
            assert "under 50 characters" in event["message"]
            ws.send_json({"type": "hangup"})
        with c.websocket_connect("/ws/browser") as ws:  # 2nd conversation this hour: allowed
            ws.send_json({"type": "start"})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "hangup"})
        with c.websocket_connect("/ws/browser") as ws:  # 3rd: refused
            ws.send_json({"type": "start"})
            assert "Too many conversations" in ws.receive_json()["message"]


# ------------------------------------------------------------------ private logs
def test_logs_keep_patient_details_out(settings, caplog, monday):
    llm = FakeLLM([tool("book_appointment", "c1", patient_name="Secret Patientname", date=monday.isoformat(),
                        time="17:00", provider_id="dr-rao", phone="9000012345")])
    _, c = build(settings, llm)
    caplog.set_level(logging.INFO)
    with c, c.websocket_connect("/ws/browser") as ws:
        ws.send_json({"type": "start"})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "text", "text": "book me"})
        for _ in range(30):
            if next_event(ws).get("type") == "tool":
                break
        ws.send_json({"type": "hangup"})
    logged = caplog.text
    assert "tool book_appointment" in logged and "'booked': True" in logged
    assert "Secret Patientname" not in logged and "9000012345" not in logged
    assert mask_phone("+919000012345") == "+91******2345"
