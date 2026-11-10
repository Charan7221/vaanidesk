"""Browser voice demo over WebSocket.

Client -> server: binary frames of 16 kHz PCM16 mono (mic), or JSON:
    {"type": "start", "language": "te-IN", "code": "<demo access code>"}
    {"type": "text", "text": "typed message"}
    {"type": "hangup"}
Server -> client: binary frames of 16 kHz PCM16 (agent voice), and JSON events:
    ready | user | assistant | tool | metrics | interrupted | clear | transfer | hangup | error
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import Counter

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..background import spawn
from ..db import DEFAULT_CLINIC, current_clinic
from ..messaging import valid_email
from ..phone import validate_phone
from ..security import client_ip, limiter
from .base import Transport

log = logging.getLogger(__name__)
router = APIRouter(tags=["browser"])


class BrowserTransport(Transport):
    channel = "browser"
    sample_rate = 16000

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._lock = asyncio.Lock()
        self.closed = False

    async def send_audio(self, pcm16: bytes) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_bytes(pcm16)
            except Exception:
                self.closed = True

    async def clear_audio(self) -> None:
        await self.send_event({"type": "clear"})

    async def send_event(self, event: dict) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
            except Exception:
                self.closed = True

    async def transfer(self, number: str, announcement: str = "") -> None:
        # In the browser demo we can't bridge a phone call; the UI shows the transfer.
        log.info("Browser demo: would transfer to %s", number)

    async def hangup(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass
        self.closed = True


_open_per_ip: Counter[str] = Counter()  # conversations currently open, per visitor address


@router.websocket("/ws/browser")
async def browser_call(ws: WebSocket) -> None:
    state = ws.app.state.container
    s = state.settings
    await ws.accept()
    transport = BrowserTransport(ws)
    session = None
    ip = client_ip(ws.headers, ws.client)
    counted = False
    typed = 0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if session:
                    await session.feed_audio(msg["bytes"])
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "start" and session is None:
                if s.demo_access_code and not secrets.compare_digest(str(data.get("code", "")), s.demo_access_code):
                    await transport.send_event({"type": "error", "message": "Invalid access code"})
                    break
                if s.missing_keys() and not state.uses_custom_providers:
                    await transport.send_event({"type": "error", "message": "Server not configured, missing: "
                                                + ", ".join(s.missing_keys())})
                    break
                if _open_per_ip[ip] >= s.chat_concurrent_per_ip:
                    await transport.send_event({"type": "error", "message": "You already have a conversation open. "
                                                "Close it first."})
                    break
                if not limiter.allow(f"chat:{ip}", limit=s.chat_sessions_per_hour, window_s=3600):
                    await transport.send_event({"type": "error", "message": "Too many conversations from your "
                                                "network. Please try again in a while."})
                    break
                _open_per_ip[ip] += 1
                counted = True
                if len(state.active_sessions) >= s.max_concurrent_calls:
                    await transport.send_event({"type": "error", "message": "All lines busy, try again shortly"})
                    break
                clinic = str(data.get("clinic") or DEFAULT_CLINIC)
                if clinic not in state.tenants:
                    await transport.send_event({"type": "error", "message": "Unknown clinic"})
                    break
                current_clinic.set(clinic)  # this call (and every task it starts) belongs to that clinic
                lang = data.get("language") if data.get("language") in state.business.languages else None
                session = await state.new_session(transport, channel="browser", language=lang,
                                                  caller=validate_phone(data.get("phone"))[0])
                session.ctx.caller_email = valid_email(data.get("email"))
                session.ctx.caller_phone_trusted = False  # typed by the visitor: proven only by a one-time code
                await transport.send_event({"type": "ready", "call_id": session.ctx.call_id})
                await session.start()
            elif kind == "text" and session and str(data.get("text", "")).strip():
                text = str(data["text"]).strip()
                if len(text) > s.chat_max_chars:
                    await transport.send_event({"type": "error", "message": f"Please keep messages under "
                                                f"{s.chat_max_chars} characters."})
                    continue
                typed += 1
                if typed > s.chat_max_messages:
                    await transport.send_event({"type": "error", "message": "This conversation is very long. "
                                                "Please start a new one, or call the clinic."})
                    break
                await session.feed_text(text)
            elif kind == "hangup":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Browser session error")
        # Show the real cause in the demo page instead of a silent hang-up.
        detail = "" if s.is_production else f" ({type(exc).__name__}: {exc})"  # internals stay in the server log
        await transport.send_event({"type": "error", "message": f"Something went wrong on our side.{detail}"})
    finally:
        transport.closed = True
        if counted:
            _open_per_ip[ip] -= 1
            if _open_per_ip[ip] <= 0:
                del _open_per_ip[ip]
        if session:
            # Finish the call record even if this handler is being cancelled (client vanished): a database
            # write interrupted half-way leaves a broken connection behind.
            await asyncio.shield(spawn(_finish(state, session), name="call-cleanup"))


async def _finish(state, session) -> None:
    try:
        await session.close()
    finally:
        state.release(session)
