"""Exotel phone calls (Indian numbers) through the Voicebot applet's bidirectional WebSocket (AgentStream).

Why Exotel: clinics in India get local virtual numbers easily, and can forward their existing number to it
when busy or unanswered, so they keep the number patients already know.

Exotel call flow to build in the Exotel dashboard (App Bazaar):
    Voicebot applet  ->  URL: wss://vaanidesk:<EXOTEL_STREAM_SECRET>@<your-host>/exotel/stream?sample-rate=16000
                          (add &clinic=<clinic-id> if this number isn't listed under the clinic's numbers)
    Passthru applet  ->  URL: https://<your-host>/exotel/after-bot   (200 = transfer to staff, 302 = hang up)
    Connect applet   ->  the clinic's reception number          (on the Passthru 200 branch)
    Hangup applet                                                (on the Passthru 302 branch)

Protocol (developer.exotel.com/docs/agentstream/websocket-protocol): JSON messages ``connected``, ``start``
(stream_sid, call_sid, from, to, custom_parameters), ``media`` (base64 raw 16-bit mono PCM, little-endian),
``dtmf``, ``mark``, ``stop``. We send ``media`` (chunks of 100 ms+, multiples of 320 bytes) and ``clear``.
The bot can't hang up or transfer itself: we close the socket and the flow continues to the next applet.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from ..background import spawn
from ..db import DEFAULT_CLINIC, Call, current_clinic
from ..phone import mask_phone, normalize_phone
from .base import Transport

log = logging.getLogger(__name__)
router = APIRouter(prefix="/exotel", tags=["exotel"])

MIN_CHUNK = 3200  # Exotel: chunks of at least 3.2 KB ...
ALIGN = 320  # ... in multiples of 320 bytes
FLUSH_AFTER_S = 0.12  # send a short tail (end of a sentence) after this much silence

#: calls (Exotel CallSid) whose bot part ended with "transfer to staff"; read by the Passthru applet
_TRANSFER: dict[str, bool] = {}


class ExotelTransport(Transport):
    channel = "phone"

    def __init__(self, ws: WebSocket, stream_sid: str, call_sid: str, sample_rate: int = 8000):
        self.ws, self.stream_sid, self.call_sid = ws, stream_sid, call_sid
        self.sample_rate = sample_rate
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self.closed = False

    async def _send(self, msg: dict) -> None:
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(msg))
            except Exception:
                self.closed = True

    async def _send_pcm(self, pcm: bytes) -> None:
        await self._send({"event": "media", "stream_sid": self.stream_sid,
                          "media": {"payload": base64.b64encode(pcm).decode()}})

    async def send_audio(self, pcm16: bytes) -> None:
        self._buf.extend(pcm16)
        while len(self._buf) >= MIN_CHUNK:
            n = len(self._buf) - len(self._buf) % ALIGN
            chunk, self._buf = bytes(self._buf[:n]), self._buf[n:]
            await self._send_pcm(chunk)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        """The last piece of a reply is usually smaller than Exotel's minimum: pad it with silence."""
        await asyncio.sleep(FLUSH_AFTER_S)
        if self._buf:
            tail = bytes(self._buf) + b"\x00" * max(0, MIN_CHUNK - len(self._buf))
            tail += b"\x00" * (-len(tail) % ALIGN)
            self._buf.clear()
            await self._send_pcm(tail)

    async def clear_audio(self) -> None:
        self._buf.clear()
        await self._send({"event": "clear", "stream_sid": self.stream_sid})

    async def transfer(self, number: str, announcement: str = "") -> None:
        _TRANSFER[self.call_sid] = True  # the Passthru applet sends the caller to the Connect (staff) applet
        await self.hangup()

    async def hangup(self) -> None:
        await asyncio.sleep(FLUSH_AFTER_S + 0.05)  # let the last words go out first
        self.closed = True
        try:
            await self.ws.close()
        except Exception:
            pass


def _authorised(ws: WebSocket, secret: str) -> bool:
    if not secret:
        return True
    header = ws.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(password, secret)


@router.websocket("/stream")
async def exotel_stream(ws: WebSocket) -> None:
    state = ws.app.state.container
    s = state.settings
    if not _authorised(ws, s.exotel_stream_secret) or (s.is_production and not s.exotel_stream_secret):
        await ws.close(code=1008)
        return
    await ws.accept()
    session = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")
            if event == "start" and session is None:
                start = msg.get("start") or {}
                params = start.get("custom_parameters") or {}
                fmt = start.get("media_format") or {}
                rate = int(fmt.get("sample_rate") or ws.query_params.get("sample-rate") or 8000)
                caller = normalize_phone(start.get("from"))
                clinic = params.get("clinic") or ws.query_params.get("clinic") or \
                    await state.clinic_for_number(normalize_phone(start.get("to")))
                current_clinic.set(clinic if clinic in state.tenants else DEFAULT_CLINIC)
                if len(state.active_sessions) >= s.max_concurrent_calls:
                    log.warning("Exotel call rejected: all lines busy")
                    _TRANSFER[start.get("call_sid", "")] = True  # send the caller to staff instead
                    break
                call_sid = start.get("call_sid", "")
                transport = ExotelTransport(ws, start.get("stream_sid") or msg.get("stream_sid", ""), call_sid, rate)
                session = await state.new_session(transport, channel="phone", caller=caller, external_id=call_sid)
                log.info("Exotel call sid=%s from=%s clinic=%s", call_sid, mask_phone(caller), current_clinic.get())
                await session.start()
            elif event == "media" and session:
                payload = (msg.get("media") or {}).get("payload")
                if payload:
                    await session.feed_audio(base64.b64decode(payload))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Exotel stream error")
    finally:
        if session:
            await asyncio.shield(spawn(_finish(state, session), name="call-cleanup"))


async def _finish(state, session) -> None:
    try:
        await session.close()
    finally:
        state.release(session)


@router.api_route("/after-bot", methods=["GET", "POST"])
async def after_bot(request: Request) -> Response:
    """Exotel Passthru applet after the Voicebot: 200 -> Connect to staff, 302 -> Hang up."""
    params = dict(request.query_params)
    if request.method == "POST":
        params.update({k: str(v) for k, v in (await request.form()).items()})
    sid = params.get("CallSid", "")
    if _TRANSFER.pop(sid, False):
        return Response("transfer", status_code=200)
    # Fallback: the call record says the AI handed over (e.g. the server restarted in between)
    state = request.app.state.container
    async with state.db.session() as s:
        from sqlalchemy import select
        call = (await s.execute(select(Call).where(Call.external_id == sid).execution_options(all_clinics=True))
                ).scalars().first()
    if call is not None and call.outcome == "transferred":
        return Response("transfer", status_code=200)
    return Response("hangup", status_code=302, headers={"Location": "/exotel/after-bot/done"})
