"""Sarvam AI text-to-speech (Bulbul) — natural Indian English, Hindi and Telugu voices.

Two modes:
  * "stream": one WebSocket per spoken response; sentences are pushed as the LLM
    generates them and audio chunks stream back (lowest latency).
    Docs: https://docs.sarvam.ai/api-reference-docs/text-to-speech/stream
  * "rest": one HTTPS request per sentence (simpler, a little slower; useful fallback).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from ..audio import resample_pcm16, strip_wav_header
from .base import TTSProvider, TTSStream

log = logging.getLogger(__name__)

SARVAM_TTS_WS = "wss://api.sarvam.ai/text-to-speech/ws"
SARVAM_TTS_REST = "https://api.sarvam.ai/text-to-speech"
SUPPORTED_RATES = (8000, 16000, 22050, 24000)


def _decode_chunk(b64: str, content_type: str, want_rate: int, got_rate: int) -> bytes:
    raw = base64.b64decode(b64)
    ctype = (content_type or "").lower()
    if "wav" in ctype or raw[:4] == b"RIFF":
        raw, rate = strip_wav_header(raw)
        got_rate = rate or got_rate
    elif "mpeg" in ctype or "mp3" in ctype:
        log.error("Sarvam returned MP3 audio; set output_audio_codec=linear16/wav. Dropping chunk.")
        return b""
    return resample_pcm16(raw, got_rate, want_rate)


class SarvamStreamTTS(TTSStream):
    def __init__(self, ws, sample_rate: int, tts_rate: int | None = None):
        self._ws = ws
        self._rate = sample_rate
        self._tts_rate = tts_rate or sample_rate
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())
        self._pinger = asyncio.create_task(self._ping_loop())
        self._closed = False

    async def send_text(self, text: str) -> None:
        if text.strip() and not self._closed:
            await self._ws.send(json.dumps({"type": "text", "data": {"text": text}}))

    async def finish(self) -> None:
        if not self._closed:
            await self._ws.send(json.dumps({"type": "flush"}))

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await self._ws.send(json.dumps({"type": "ping"}))
        except Exception:
            pass

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mtype, data = msg.get("type"), msg.get("data") or {}
                if mtype == "audio":
                    pcm = _decode_chunk(data.get("audio", ""), data.get("content_type", ""), self._rate, self._tts_rate)
                    if pcm:
                        await self._queue.put(pcm)
                elif mtype == "event" and data.get("event_type") == "final":
                    break
                elif mtype == "error":
                    log.error("Sarvam TTS error: %s", data.get("message"))
                    break
        except Exception as exc:
            if not self._closed:
                log.warning("Sarvam TTS connection ended: %s", exc)
        finally:
            await self._queue.put(None)

    async def audio(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        self._closed = True
        self._pinger.cancel()
        self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


class SarvamRestTTS(TTSStream):
    """Fallback: synthesise each sentence with a REST call, in order."""

    def __init__(self, provider: SarvamTTS, language: str, sample_rate: int):
        self._p = provider
        self._lang = language
        self._rate = sample_rate
        self._texts: asyncio.Queue[str | None] = asyncio.Queue()
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._worker = asyncio.create_task(self._run())

    async def send_text(self, text: str) -> None:
        if text.strip():
            await self._texts.put(text)

    async def finish(self) -> None:
        await self._texts.put(None)

    async def _run(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                while (text := await self._texts.get()) is not None:
                    rate = self._rate if self._rate in SUPPORTED_RATES else 16000
                    resp = await client.post(
                        SARVAM_TTS_REST,
                        headers={"api-subscription-key": self._p.api_key},
                        json={
                            "text": text,
                            "target_language_code": self._lang,
                            "speaker": self._p.speaker,
                            "model": self._p.model,
                            "pace": self._p.pace,
                            "speech_sample_rate": rate,
                        },
                    )
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Sarvam TTS HTTP {resp.status_code}: {resp.text[:300]}")
                    for b64 in resp.json().get("audios", []):
                        pcm = _decode_chunk(b64, "audio/wav", self._rate, rate)
                        if pcm:
                            await self._out.put(pcm)
        except Exception as exc:
            log.error("Sarvam REST TTS failed: %s", exc)
        finally:
            await self._out.put(None)

    async def audio(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._out.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        self._worker.cancel()


class SarvamTTS(TTSProvider):
    def __init__(self, api_key: str, model: str = "bulbul:v3", speaker: str = "priya",
                 pace: float = 1.0, mode: str = "stream"):
        self.api_key = api_key
        self.model = model
        self.speaker = speaker
        self.pace = pace
        self.mode = mode

    async def open(self, language: str, sample_rate: int) -> TTSStream:
        if self.mode == "rest":
            return SarvamRestTTS(self, language, sample_rate)
        rate = sample_rate if sample_rate in SUPPORTED_RATES else 16000
        url = f"{SARVAM_TTS_WS}?{urlencode({'model': self.model, 'send_completion_event': 'true'})}"
        ws = await connect(url, additional_headers={"Api-Subscription-Key": self.api_key}, max_size=None)
        await ws.send(json.dumps({
            "type": "config",
            "data": {
                "language_code": language,
                "speaker": self.speaker,
                "pace": self.pace,
                "speech_sample_rate": rate,
                "output_audio_codec": "linear16",
                "min_buffer_size": 30,
                "max_chunk_length": 150,
            },
        }))
        return SarvamStreamTTS(ws, sample_rate, rate)
