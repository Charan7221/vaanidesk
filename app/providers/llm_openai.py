"""OpenAI chat-completions LLM with streaming text + tool calling."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI, BadRequestError, RateLimitError

from .base import LLMChunk, LLMProvider, ToolCall

log = logging.getLogger(__name__)


def _deep_merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        elif isinstance(value, str) and isinstance(dst.get(key), str):
            dst[key] += value  # streamed string fragments
        else:
            dst[key] = value


def _retry_after(exc: Exception) -> float | None:
    """Seconds the provider asked us to wait (Gemini: 'retry in 32.5s' or, for daily quotas, 'retry in 6h44m20s')."""
    m = re.search(r"retry in\s+((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", str(exc), re.IGNORECASE)
    if not m or not m.group(1):
        return None
    parts = re.findall(r"([\d.]+)([hms])", m.group(1))
    return sum(float(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts) or None


class OpenAILLM(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4.1-mini", temperature: float = 0.4,
                 client: AsyncOpenAI | None = None, base_url: str | None = None,
                 max_retry_wait_s: float = 6.0, max_retries: int = 3, reasoning_effort: str | None = None):
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model
        self.temperature = temperature
        # Free tiers have per-minute limits (Gemini free: 15 requests/min). On a 429 we wait and
        # retry, but never longer than max_retry_wait_s per attempt (a live caller is waiting).
        self.max_retry_wait_s = max_retry_wait_s
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort

    async def _create(self, **kwargs):
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                # Model doesn't support this thinking level: warn once, then carry on without it.
                if "reasoning_effort" in kwargs and "reason" in str(exc).lower():
                    log.warning("Model rejected LLM_REASONING_EFFORT=%s (%s); ignoring it", self.reasoning_effort, exc)
                    self.reasoning_effort = None
                    kwargs.pop("reasoning_effort")
                    continue
                raise
            except RateLimitError as exc:
                wait = _retry_after(exc) or 2.0 * (attempt + 1)
                if attempt == self.max_retries or wait > self.max_retry_wait_s:
                    raise
                log.warning("LLM rate limited; retrying in %.1fs (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait + 0.5)

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[LLMChunk]:
        kwargs = {"model": self.model, "messages": messages, "stream": True, "temperature": self.temperature}
        if tools:
            kwargs["tools"] = tools
        response = await self._create(**kwargs)

        pending: dict[int, dict] = {}  # index -> {id, name, args}
        async for event in response:
            if not event.choices:
                continue
            delta = event.choices[0].delta
            if delta.content:
                yield LLMChunk(text=delta.content)
            for pos, tc in enumerate(delta.tool_calls or []):
                # Some OpenAI-compatible APIs (e.g. Gemini) omit `index` and send whole calls at once.
                idx = tc.index if getattr(tc, "index", None) is not None else len(pending) + pos
                slot = pending.setdefault(idx, {"id": "", "name": "", "args": "", "extra": {}})
                # Keep unknown fields (Gemini sends extra_content.google.thought_signature here and
                # rejects the next request unless it is echoed back).
                _deep_merge(slot["extra"], getattr(tc, "model_extra", None) or {})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

        if pending:
            calls = []
            for _, slot in sorted(pending.items()):
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    log.warning("Bad tool arguments from LLM: %s", slot["args"])
                    args = {}
                calls.append(ToolCall(id=slot["id"] or f"call_{len(calls)}", name=slot["name"], arguments=args,
                                      extra=slot["extra"]))
            yield LLMChunk(tool_calls=calls)
