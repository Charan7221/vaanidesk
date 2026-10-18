"""Provider interfaces for speech-to-text, LLM and text-to-speech.

Every provider is swappable: the conversation engine only depends on these types.
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------- STT
@dataclass
class STTEvent:
    """Event emitted by a streaming speech-to-text session.

    kind:
      - "speech_started": caller started talking (used for barge-in)
      - "interim": partial transcript (may change)
      - "final": a finalised transcript segment
      - "utterance_end": the caller finished their turn
      - "error": provider error (text holds the message)
    """

    kind: Literal["speech_started", "interim", "final", "utterance_end", "error"]
    text: str = ""
    language: str | None = None


class STTStream(abc.ABC):
    """A live transcription session. Feed audio in, iterate events out."""

    @abc.abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abc.abstractmethod
    def events(self) -> AsyncIterator[STTEvent]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class STTProvider(abc.ABC):
    @abc.abstractmethod
    async def open(self, sample_rate: int, language: str | None = None) -> STTStream: ...


# ---------------------------------------------------------------- LLM
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    #: provider-specific fields that must be sent back unchanged, e.g. Gemini 3's
    #: {"extra_content": {"google": {"thought_signature": "..."}}}
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMChunk:
    """Streaming LLM output: either a text delta or a completed tool call list."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[LLMChunk]: ...

    async def complete(self, messages: list[dict]) -> str:
        """Non-streaming helper used for call summaries."""
        out = []
        async for chunk in self.stream(messages, tools=[]):
            out.append(chunk.text)
        return "".join(out)


# ---------------------------------------------------------------- TTS
class TTSStream(abc.ABC):
    """One spoken response. Push sentences in, iterate PCM16 audio out."""

    @abc.abstractmethod
    async def send_text(self, text: str) -> None: ...

    @abc.abstractmethod
    async def finish(self) -> None:
        """No more text will be sent for this response."""

    @abc.abstractmethod
    def audio(self) -> AsyncIterator[bytes]:
        """PCM16 mono audio at the requested sample rate. Ends after finish()."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Abort immediately (used for barge-in)."""


class TTSProvider(abc.ABC):
    @abc.abstractmethod
    async def open(self, language: str, sample_rate: int) -> TTSStream: ...
