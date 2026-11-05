"""Exotel Voicebot (AgentStream) WebSocket: audio framing, clinic routing, auth, and the after-bot Passthru."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.state import AppState
from app.transports import exotel

from .fakes import FakeLLM, FakeSTT, FakeTTS

AUTH = ("admin", "secret")


def make(settings, **over):
    s = settings.model_copy(update=over)
    return TestClient(create_app(s, AppState.build(s, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())))


def start_msg(**extra):
    return {"event": "start", "sequence_number": 1, "stream_sid": "S1",
            "start": {"stream_sid": "S1", "call_sid": "EXO1", "account_sid": "acc", "from": "09876543210",
                      "to": "08012345678", "custom_parameters": {},
                      "media_format": {"encoding": "raw", "sample_rate": 16000, "bit_rate": "128"}, **extra}}


def test_exotel_call_end_to_end(settings):
    with make(settings) as c:
        with c.websocket_connect("/exotel/stream?sample-rate=16000") as ws:
            ws.send_json({"event": "connected"})
            ws.send_json(start_msg())
            msg = json.loads(ws.receive_text())  # the greeting comes back as Exotel media chunks
            assert msg["event"] == "media" and msg["stream_sid"] == "S1"
            pcm = base64.b64decode(msg["media"]["payload"])
            assert len(pcm) >= exotel.MIN_CHUNK and len(pcm) % exotel.ALIGN == 0
            ws.send_json({"event": "media", "stream_sid": "S1",
                          "media": {"chunk": 1, "timestamp": "100", "payload": base64.b64encode(b"\0\0" * 1600).decode()}})
            ws.send_json({"event": "stop", "stop": {"call_sid": "EXO1", "reason": "callended"}})
        calls = c.get("/api/calls", auth=AUTH).json()
        assert calls[0]["channel"] == "phone" and calls[0]["caller"] == "+919876543210"


def test_exotel_secret_is_required_when_set(settings):
    with make(settings, exotel_stream_secret="s3cret") as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/exotel/stream") as ws:
                ws.receive_text()
        good = base64.b64encode(b"vaanidesk:s3cret").decode()
        with c.websocket_connect("/exotel/stream", headers={"Authorization": f"Basic {good}"}) as ws:
            ws.send_json(start_msg())
            assert json.loads(ws.receive_text())["event"] == "media"
            ws.send_json({"event": "stop"})


def test_passthru_after_bot_branches(settings):
    with make(settings) as c:
        exotel._TRANSFER["EXO9"] = True
        assert c.get("/exotel/after-bot?CallSid=EXO9", follow_redirects=False).status_code == 200
        assert c.get("/exotel/after-bot?CallSid=EXO9", follow_redirects=False).status_code == 302
        assert c.post("/exotel/after-bot", data={"CallSid": "nope"}, follow_redirects=False).status_code == 302


async def test_framing_pads_the_last_piece():
    sent = []

    class WS:
        async def send_text(self, t):
            sent.append(json.loads(t))

    t = exotel.ExotelTransport(WS(), "S", "C", 8000)
    await t.send_audio(b"\x01\x00" * 2000)  # 4000 bytes -> one 3840-byte chunk now, 160 bytes later
    assert [len(base64.b64decode(m["media"]["payload"])) for m in sent] == [3840]
    await asyncio.sleep(exotel.FLUSH_AFTER_S + 0.1)
    tail = base64.b64decode(sent[-1]["media"]["payload"])
    assert len(tail) == exotel.MIN_CHUNK and tail[:160] == b"\x01\x00" * 80
    await t.clear_audio()
    assert sent[-1] == {"event": "clear", "stream_sid": "S"}
