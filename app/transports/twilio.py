"""Twilio phone calls: incoming-call webhook (TwiML) + bidirectional Media Stream WebSocket.

Flow: caller dials your Twilio number -> Twilio POSTs /twilio/voice -> we answer with
<Connect><Stream> -> Twilio opens wss://.../twilio/stream and sends 8 kHz mu-law audio.
Docs: https://www.twilio.com/docs/voice/media-streams
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from ..audio import pcm16_to_ulaw, ulaw_to_pcm16
from ..security import is_valid_twilio_request
from ..tools.twilio_client import TwilioClient
from .base import Transport

log = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio", tags=["twilio"])

FRAME_BYTES = 160 * 25  # send audio to Twilio in 0.5 s mu-law chunks


class TwilioTransport(Transport):
    channel = "phone"
    sample_rate = 8000

    def __init__(self, ws: WebSocket, stream_sid: str, call_sid: str, twilio: TwilioClient):
        self.ws = ws
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.twilio = twilio
        self._lock = asyncio.Lock()

    async def _send(self, msg: dict) -> None:
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(msg))
            except Exception:
                pass  # call already ended

    async def send_audio(self, pcm16: bytes) -> None:
        ulaw = pcm16_to_ulaw(pcm16)
        for i in range(0, len(ulaw), FRAME_BYTES):
            payload = base64.b64encode(ulaw[i : i + FRAME_BYTES]).decode()
            await self._send({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})

    async def clear_audio(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def transfer(self, number: str, announcement: str = "") -> None:
        if number:
            await self.twilio.transfer_call(self.call_sid, number, announcement)

    async def hangup(self) -> None:
        await self.twilio.hangup_call(self.call_sid)


def _twiml_stream(ws_url: str, params: dict[str, str]) -> str:
    param_xml = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in params.items() if v)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f"<Stream url={quoteattr(ws_url)}>{param_xml}</Stream></Connect></Response>")


@router.post("/voice")
async def incoming_call(request: Request) -> Response:
    state = request.app.state.container
    s = state.settings
    form = {k: str(v) for k, v in (await request.form()).items()}
    if s.twilio_validate_signature and s.twilio_auth_token:
        url = s.public_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        if not is_valid_twilio_request(s.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature")):
            raise HTTPException(403, "Invalid Twilio signature")

    if s.missing_keys() and not state.uses_custom_providers:
        log.error("Incoming call rejected; missing keys: %s", s.missing_keys())
        unavailable = ('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, our assistant is not '
                       "available right now. Please call back later.</Say><Hangup/></Response>")
        return Response(unavailable, media_type="application/xml")

    if len(state.active_sessions) >= s.max_concurrent_calls:
        busy = ('<?xml version="1.0" encoding="UTF-8"?><Response><Say>Sorry, all our lines are busy. '
                "Please call again in a few minutes.</Say><Hangup/></Response>")
        return Response(busy, media_type="application/xml")

    xml = _twiml_stream(f"{s.public_ws_base.rstrip('/')}/twilio/stream",
                        {"from": form.get("From", ""), "callSid": form.get("CallSid", "")})
    return Response(xml, media_type="application/xml")


@router.websocket("/stream")
async def media_stream(ws: WebSocket) -> None:
    state = ws.app.state.container
    await ws.accept()
    session = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")
            if event == "start":
                start = msg["start"]
                params = start.get("customParameters") or {}
                call_sid = start.get("callSid") or params.get("callSid", "")
                transport = TwilioTransport(ws, start["streamSid"], call_sid, state.twilio)
                session = await state.new_session(transport, channel="phone", caller=params.get("from") or None,
                                                  external_id=call_sid)
                log.info("Call started sid=%s from=%s call_id=%s", call_sid, params.get("from"), session.ctx.call_id)
                await session.start()
            elif event == "media" and session:
                if msg["media"].get("track", "inbound") == "inbound":
                    await session.feed_audio(ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"])))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Twilio stream error")
    finally:
        if session:
            await session.close()
            state.release(session)
