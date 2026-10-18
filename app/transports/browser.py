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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..phone import validate_phone
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


@router.websocket("/ws/browser")
async def browser_call(ws: WebSocket) -> None:
    state = ws.app.state.container
    s = state.settings
    await ws.accept()
    transport = BrowserTransport(ws)
    session = None
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
                if len(state.active_sessions) >= s.max_concurrent_calls:
                    await transport.send_event({"type": "error", "message": "All lines busy, try again shortly"})
                    break
                lang = data.get("language") if data.get("language") in state.business.languages else None
                session = await state.new_session(transport, channel="browser", language=lang,
                                                  caller=validate_phone(data.get("phone"))[0])
                await transport.send_event({"type": "ready", "call_id": session.ctx.call_id})
                await session.start()
            elif kind == "text" and session and data.get("text", "").strip():
                await session.feed_text(data["text"].strip())
            elif kind == "hangup":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Browser session error")
        # Show the real cause in the demo page instead of a silent hang-up.
        await transport.send_event({"type": "error", "message": f"Server error: {type(exc).__name__}: {exc}"})
    finally:
        transport.closed = True
        if session:
            await session.close()
            state.release(session)
