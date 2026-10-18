"""Text embeddings through any OpenAI-compatible API (Gemini, OpenAI, ...)."""
from __future__ import annotations

import abc
import logging

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


class Embedder(abc.ABC):
    model: str = ""

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder(Embedder):
    def __init__(self, api_key: str, model: str, base_url: str | None = None, batch_size: int = 64,
                 client: AsyncOpenAI | None = None):
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.batch_size = batch_size

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = await self.client.embeddings.create(model=self.model, input=batch)
            data = list(resp.data)
            # OpenAI sets `index` on each item; Gemini's compatible API leaves it empty (in order already).
            if all(getattr(d, "index", None) is not None for d in data):
                data.sort(key=lambda d: d.index)
            if len(data) != len(batch):
                raise ValueError(f"embedding API returned {len(data)} vectors for {len(batch)} texts")
            out.extend(list(d.embedding) for d in data)
        return out


def default_embedding_model(base_url: str) -> str:
    if "generativelanguage" in base_url:
        return "gemini-embedding-001"
    if "groq" in base_url:
        return ""  # Groq has no embeddings endpoint: keyword search only
    return "text-embedding-3-small"
