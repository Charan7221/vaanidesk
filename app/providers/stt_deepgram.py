"""Deepgram streaming speech-to-text (Nova-3) over WebSocket.

Docs: https://developers.deepgram.com/docs/live-streaming-audio
Best latency for English / Hindi-English code-switching ("multi").
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from .base import STTEvent, STTProvider, STTStream

log = logging.getLogger(__name__)

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
_LANG_MAP = {"en": "en-IN", "hi": "hi-IN", "te": "te-IN"}


def _normalise_lang(code: str | None) -> str | None:
    if not code:
        return None
    base = code.split("-")[0].lower()
    return _LANG_MAP.get(base, code)


class DeepgramSTTStream(STTStream):
    def __init__(self, ws: ClientConnection, default_language: str | None):
        self._ws = ws
        self._lang = default_language
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())
        self._keepalive = asyncio.create_task(self._keepalive_loop())
        self._closed = False

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed:
            return
        try:
            await self._ws.send(pcm16)
        except Exception as exc:
            await self._queue.put(STTEvent("error", text=str(exc)))
            self._closed = True

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(8)
                await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            pass

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "SpeechStarted":
                    await self._queue.put(STTEvent("speech_started"))
                elif mtype == "Results":
                    alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                    text = (alt.get("transcript") or "").strip()
                    langs = alt.get("languages") or []
                    lang = _normalise_lang(langs[0] if langs else None) or self._lang
                    if text:
                        kind = "final" if msg.get("is_final") else "interim"
                        await self._queue.put(STTEvent(kind, text=text, language=lang))
                    if msg.get("speech_final"):
                        await self._queue.put(STTEvent("utterance_end", language=lang))
                elif mtype == "UtteranceEnd":
                    await self._queue.put(STTEvent("utterance_end", language=self._lang))
        except Exception as exc:
            if not self._closed:
                log.warning("Deepgram connection ended: %s", exc)
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
        self._keepalive.cancel()
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
            await self._ws.close()
        except Exception:
            pass
        self._reader.cancel()


class DeepgramSTT(STTProvider):
    def __init__(self, api_key: str, model: str = "nova-3", language: str = "multi"):
        self.api_key = api_key
        self.model = model
        self.language = language

    async def open(self, sample_rate: int, language: str | None = None) -> STTStream:
        requested = language or self.language
        lang = "multi" if requested == "multi" else requested.split("-")[0]
        params = {
            "model": self.model,
            "language": lang,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
            "endpointing": "300",
            "utterance_end_ms": "1000",
            "vad_events": "true",
        }
        url = f"{DEEPGRAM_URL}?{urlencode(params)}"
        ws = await connect(url, additional_headers={"Authorization": f"Token {self.api_key}"}, max_size=None)
        default_lang = None if lang == "multi" else _normalise_lang(lang)
        return DeepgramSTTStream(ws, default_lang)
