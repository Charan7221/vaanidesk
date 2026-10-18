"""In-memory fakes for STT / LLM / TTS / transport so the pipeline can be tested offline."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from app.providers.base import LLMChunk, LLMProvider, STTEvent, STTProvider, STTStream, ToolCall, TTSProvider, TTSStream
from app.transports.base import Transport


class FakeSTTStream(STTStream):
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.audio_bytes = 0

    async def send_audio(self, pcm16: bytes) -> None:
        self.audio_bytes += len(pcm16)

    async def push(self, *events: STTEvent) -> None:
        for e in events:
            await self.queue.put(e)

    async def events(self) -> AsyncIterator[STTEvent]:
        while (ev := await self.queue.get()) is not None:
            yield ev

    async def close(self) -> None:
        await self.queue.put(None)


class FakeSTT(STTProvider):
    def __init__(self):
        self.streams: list[FakeSTTStream] = []

    async def open(self, sample_rate: int, language: str | None = None) -> STTStream:
        s = FakeSTTStream()
        self.streams.append(s)
        return s


Script = Callable[[list[dict]], list[LLMChunk]]


class FakeLLM(LLMProvider):
    """Replays scripted turns. Each script entry is a list of chunks or a callable(messages)->chunks."""

    def __init__(self, script: list[list[LLMChunk] | Script] | None = None, delay: float = 0.0):
        self.script = list(script or [])
        self.calls: list[list[dict]] = []
        self.delay = delay

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[LLMChunk]:
        self.calls.append([dict(m) for m in messages])
        if not tools:  # summary request
            yield LLMChunk(text="Caller booked an appointment.")
            return
        step = self.script.pop(0) if self.script else [LLMChunk(text="Okay.")]
        chunks = step(messages) if callable(step) else step
        for c in chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield c


def text(*parts: str) -> list[LLMChunk]:
    return [LLMChunk(text=p) for p in parts]


def tool(name: str, call_id: str = "call_1", **args) -> list[LLMChunk]:
    return [LLMChunk(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])]


class FakeTTSStream(TTSStream):
    def __init__(self, language: str, rate: int, chunk_delay: float):
        self.language = language
        self.rate = rate
        self.texts: list[str] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.chunk_delay = chunk_delay
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.texts.append(text)
        # 10 ms of audio per character
        n = int(self.rate * 0.01) * 2 * max(1, len(text.strip()))
        if self.chunk_delay:
            await asyncio.sleep(self.chunk_delay)
        await self.q.put(b"\x01\x00" * (n // 2))

    async def finish(self) -> None:
        await self.q.put(None)

    async def audio(self) -> AsyncIterator[bytes]:
        while (c := await self.q.get()) is not None:
            yield c

    async def close(self) -> None:
        self.closed = True


class FakeTTS(TTSProvider):
    def __init__(self, chunk_delay: float = 0.0):
        self.streams: list[FakeTTSStream] = []
        self.chunk_delay = chunk_delay

    async def open(self, language: str, sample_rate: int) -> TTSStream:
        s = FakeTTSStream(language, sample_rate, self.chunk_delay)
        self.streams.append(s)
        return s


class FakeTransport(Transport):
    channel = "browser"
    sample_rate = 16000

    def __init__(self):
        self.audio = bytearray()
        self.events: list[dict] = []
        self.cleared = 0
        self.transferred_to: str | None = None
        self.hung_up = False

    async def send_audio(self, pcm16: bytes) -> None:
        self.audio.extend(pcm16)

    async def clear_audio(self) -> None:
        self.cleared += 1

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def transfer(self, number: str, announcement: str = "") -> None:
        self.transferred_to = number

    async def hangup(self) -> None:
        self.hung_up = True

    def of_type(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]
