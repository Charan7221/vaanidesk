"""Sarvam AI streaming speech-to-text (Saaras) over WebSocket.

Docs: https://docs.sarvam.ai/api-reference-docs/speech-to-text/transcribe/ws
Auto-detects English / Hindi / Telugu (and 20 more Indian languages) per utterance.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from .base import STTEvent, STTProvider, STTStream

log = logging.getLogger(__name__)

SARVAM_STT_URL = "wss://api.sarvam.ai/speech-to-text/ws"


class SarvamSTTStream(STTStream):
    # Batch small telephony frames (20 ms) into ~100 ms messages.
    BATCH_MS = 100

    def __init__(self, ws: ClientConnection, sample_rate: int):
        self._ws = ws
        self._rate = sample_rate
        self._buf = bytearray()
        self._batch_bytes = int(sample_rate * 2 * self.BATCH_MS / 1000)
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())
        self._closed = False

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(pcm16)
        if len(self._buf) >= self._batch_bytes:
            chunk, self._buf = bytes(self._buf), bytearray()
            msg = {"audio": {"data": base64.b64encode(chunk).decode(), "sample_rate": str(self._rate),
                             "encoding": "audio/wav"}}
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as exc:  # connection dropped
                log.warning("Sarvam STT send failed: %s", exc)
                await self._queue.put(STTEvent("error", text=str(exc)))
                self._closed = True

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mtype, data = msg.get("type"), msg.get("data") or {}
                if mtype == "events":
                    signal = data.get("signal_type")
                    if signal == "START_SPEECH":
                        await self._queue.put(STTEvent("speech_started"))
                elif mtype == "data":
                    text = (data.get("transcript") or "").strip()
                    if text:
                        lang = data.get("language_code")
                        await self._queue.put(STTEvent("final", text=text, language=lang))
                        await self._queue.put(STTEvent("utterance_end", language=lang))
                elif mtype == "error":
                    await self._queue.put(STTEvent("error", text=str(data.get("error") or data)))
        except Exception as exc:
            if not self._closed:
                log.warning("Sarvam STT connection ended: %s", exc)
        finally:
            await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass
        self._reader.cancel()


class SarvamSTT(STTProvider):
    def __init__(self, api_key: str, model: str = "saaras:v4"):
        self.api_key = api_key
        self.model = model

    async def open(self, sample_rate: int, language: str | None = None) -> STTStream:
        params = {
            "model": self.model,
            "mode": "transcribe",
            "language-code": language or "unknown",  # unknown = auto-detect per utterance
            "sample_rate": str(sample_rate),
            "input_audio_codec": "pcm_s16le",
            "vad_signals": "true",
            "high_vad_sensitivity": "true",
        }
        url = f"{SARVAM_STT_URL}?{urlencode(params)}"
        ws = await connect(url, additional_headers={"Api-Subscription-Key": self.api_key}, max_size=None)
        return SarvamSTTStream(ws, sample_rate)
