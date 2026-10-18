"""HTTP / WebSocket API tests with fake AI providers."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.state import AppState

from .fakes import FakeLLM, FakeSTT, FakeTTS, text

AUTH = ("admin", "secret")


@pytest.fixture
def client(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM([text("We open at nine.")]), tts=FakeTTS())
    app = create_app(settings, state)
    with TestClient(app) as c:
        yield c


def test_health_and_public_config(client):
    assert client.get("/health").json()["status"] == "ok"
    cfg = client.get("/api/public-config").json()
    assert cfg["languages"] == ["en-IN", "hi-IN", "te-IN"]


def test_dashboard_requires_auth(client):
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/stats", auth=AUTH).json()["total_calls"] == 0
    assert client.get("/dashboard", auth=AUTH).status_code == 200
    assert client.get("/demo").status_code == 200


def test_twilio_webhook_returns_stream_twiml(client):
    r = client.post("/twilio/voice", data={"From": "+919876543210", "CallSid": "CA123"})
    assert r.status_code == 200
    assert "<Connect><Stream url=\"ws://localhost:8000/twilio/stream\">" in r.text
    assert 'value="+919876543210"' in r.text


def test_twilio_webhook_rejects_bad_signature(settings):
    s = settings.model_copy(update={"twilio_validate_signature": True, "twilio_auth_token": "tok"})
    app = create_app(s, AppState.build(s, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS()))
    with TestClient(app) as c:
        assert c.post("/twilio/voice", data={"From": "+1"}).status_code == 403


def test_browser_call_end_to_end(client):
    with client.websocket_connect("/ws/browser") as ws:
        ws.send_json({"type": "start", "language": "en-IN"})
        assert ws.receive_json()["type"] == "ready"
        greeting = ws.receive_json()
        assert greeting["type"] == "assistant" and greeting["final"]
        assert isinstance(ws.receive_bytes(), bytes)  # greeting audio
        ws.send_bytes(b"\x00\x00" * 640)  # mic audio is accepted
        ws.send_json({"type": "text", "text": "When do you open?"})
        seen = []
        deadline = time.time() + 5
        while time.time() < deadline:
            msg = ws.receive()
            if msg.get("text"):
                import json
                ev = json.loads(msg["text"])
                seen.append(ev)
                if ev["type"] == "assistant" and ev.get("final") and "nine" in ev["text"]:
                    break
        assert any(e["type"] == "metrics" for e in seen)
        ws.send_json({"type": "hangup"})

    calls = client.get("/api/calls", auth=AUTH).json()
    assert len(calls) == 1 and calls[0]["channel"] == "browser"
    detail = client.get(f"/api/calls/{calls[0]['id']}", auth=AUTH).json()
    roles = [t["role"] for t in detail["turns"]]
    assert roles[:3] == ["assistant", "user", "assistant"]


def test_twilio_media_stream_end_to_end(client):
    import base64
    import json

    from app.audio import pcm16_to_ulaw

    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA9",
                                                  "customParameters": {"from": "+919876543210"}}})
        # The greeting is streamed back as mu-law media frames for this stream
        msg = json.loads(ws.receive_text())
        assert msg["event"] == "media" and msg["streamSid"] == "MZ1"
        assert len(base64.b64decode(msg["media"]["payload"])) > 0
        ulaw = pcm16_to_ulaw(b"\x00\x00" * 160)
        ws.send_json({"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(ulaw).decode()}})
        ws.send_json({"event": "stop"})

    calls = client.get("/api/calls", auth=AUTH).json()
    assert calls[0]["channel"] == "phone" and calls[0]["caller"] == "+919876543210"


def test_demo_access_code(settings):
    s = settings.model_copy(update={"demo_access_code": "letmein"})
    app = create_app(s, AppState.build(s, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS()))
    with TestClient(app) as c, c.websocket_connect("/ws/browser") as ws:
        ws.send_json({"type": "start", "code": "wrong"})
        assert ws.receive_json() == {"type": "error", "message": "Invalid access code"}


def test_stats_period_and_failed_calls(client):
    db = client.app.state.container.db
    # a call that never finished (e.g. server crashed while starting) is reported as failed, not answered
    client.portal.call(db.create_call, "browser")
    s = client.get("/api/stats?days=1", auth=AUTH).json()
    assert s["total_calls"] == 0 and s["failed_calls"] == 1 and "since" in s
    calls = client.get("/api/calls?days=1", auth=AUTH).json()
    assert calls[0]["outcome"] == "failed"
