"""Transport abstraction: how audio reaches / leaves the caller (phone or browser)."""
from __future__ import annotations

import abc


class Transport(abc.ABC):
    channel: str = "unknown"
    #: sample rate of PCM16 audio exchanged with the conversation engine
    sample_rate: int = 16000

    @abc.abstractmethod
    async def send_audio(self, pcm16: bytes) -> None:
        """Play PCM16 mono audio to the caller."""

    @abc.abstractmethod
    async def clear_audio(self) -> None:
        """Stop and discard any audio still buffered for playback (barge-in)."""

    async def send_event(self, event: dict) -> None:
        """Optional: push UI events (transcripts, metrics). Phones ignore these."""

    async def transfer(self, number: str, announcement: str = "") -> None:
        """Hand the caller to a human."""

    async def hangup(self) -> None:
        """End the call."""
