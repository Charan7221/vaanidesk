"""Protocol tests for the real provider clients against local mock WebSocket servers."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from app.providers import llm_openai, stt_deepgram, stt_sarvam, tts_sarvam


@pytest.fixture
async def mock_server():
    """Start a WS server running ``handler``; yields a function returning its ws:// URL."""
    servers = []

    async def start(handler):
        server = await serve(handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/"

    yield start
    for s in servers:
        s.close()
        await s.wait_closed()


async def collect(aiter, n=None, timeout=3):
    out = []

    async def run():
        async for x in aiter:
            out.append(x)
            if n and len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_sarvam_stt_protocol(mock_server, monkeypatch):
    received = {}

    async def handler(ws):
        received["headers"] = dict(ws.request.headers)
        received["path"] = ws.request.path
        msg = json.loads(await ws.recv())
        received["audio"] = base64.b64decode(msg["audio"]["data"])
        await ws.send(json.dumps({"type": "events", "data": {"signal_type": "START_SPEECH"}}))
        await ws.send(json.dumps({"type": "events", "data": {"signal_type": "END_SPEECH"}}))
        await ws.send(json.dumps({"type": "data", "data": {"transcript": "నమస్కారం", "language_code": "te-IN"}}))
        await asyncio.sleep(0.5)

    url = await mock_server(handler)
    monkeypatch.setattr(stt_sarvam, "SARVAM_STT_URL", url.rstrip("/"))
    stream = await stt_sarvam.SarvamSTT("key123").open(8000)
    await stream.send_audio(b"\x01\x00" * 800)  # exactly 100 ms -> one message
    events = await collect(stream.events(), n=3)
    await stream.close()

    assert received["headers"]["api-subscription-key"] == "key123"
    assert "language-code=unknown" in received["path"] and "sample_rate=8000" in received["path"]
    assert received["audio"] == b"\x01\x00" * 800
    assert [e.kind for e in events] == ["speech_started", "final", "utterance_end"]
    assert events[1].text == "నమస్కారం" and events[1].language == "te-IN"


async def test_deepgram_protocol(mock_server, monkeypatch):
    received = {}

    async def handler(ws):
        received["headers"] = dict(ws.request.headers)
        received["audio"] = await ws.recv()
        await ws.send(json.dumps({"type": "SpeechStarted"}))
        await ws.send(json.dumps({"type": "Results", "is_final": False, "speech_final": False,
                                  "channel": {"alternatives": [{"transcript": "book an"}]}}))
        await ws.send(json.dumps({"type": "Results", "is_final": True, "speech_final": True,
                                  "channel": {"alternatives": [{"transcript": "book an appointment",
                                                                "languages": ["hi"]}]}}))
        await asyncio.sleep(0.5)

    url = await mock_server(handler)
    monkeypatch.setattr(stt_deepgram, "DEEPGRAM_URL", url.rstrip("/"))
    stream = await stt_deepgram.DeepgramSTT("dgkey").open(16000)
    await stream.send_audio(b"\x00\x00" * 160)
    events = await collect(stream.events(), n=4)
    await stream.close()

    assert received["headers"]["authorization"] == "Token dgkey"
    assert received["audio"] == b"\x00\x00" * 160
    assert [e.kind for e in events] == ["speech_started", "interim", "final", "utterance_end"]
    assert events[2].language == "hi-IN"


async def test_sarvam_tts_stream_protocol(mock_server, monkeypatch):
    received = []
    pcm = b"\x10\x00" * 160

    async def handler(ws):
        while True:
            msg = json.loads(await ws.recv())
            received.append(msg)
            if msg["type"] == "flush":
                await ws.send(json.dumps({"type": "audio", "data": {"content_type": "audio/l16",
                                                                    "audio": base64.b64encode(pcm).decode()}}))
                await ws.send(json.dumps({"type": "event", "data": {"event_type": "final"}}))
                await asyncio.sleep(0.2)
                return

    url = await mock_server(handler)
    monkeypatch.setattr(tts_sarvam, "SARVAM_TTS_WS", url.rstrip("/"))
    stream = await tts_sarvam.SarvamTTS("k", speaker="priya").open("hi-IN", 16000)
    await stream.send_text("नमस्ते।")
    await stream.finish()
    audio = await collect(stream.audio())
    await stream.close()

    assert received[0]["type"] == "config"
    assert received[0]["data"]["language_code"] == "hi-IN"
    assert received[0]["data"]["output_audio_codec"] == "linear16"
    assert received[0]["data"]["speech_sample_rate"] == 16000
    assert received[1] == {"type": "text", "data": {"text": "नमस्ते।"}}
    assert b"".join(audio) == pcm


async def test_openai_streaming_tool_call_assembly():
    def delta(content=None, tool_calls=None):
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))])

    def tc(index, id=None, name=None, args=None, extra=None):
        return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args),
                               model_extra=extra)

    events = [
        delta("Let me check. "),
        delta(tool_calls=[tc(0, "call_1", "check_availability", '{"da',
                             extra={"extra_content": {"google": {"thought_signature": "SIG=="}}})]),
        delta(tool_calls=[tc(0, None, None, 'te": "2026-10-19"}')]),
        SimpleNamespace(choices=[]),
    ]

    class FakeCompletions:
        async def create(self, **kwargs):
            self.kwargs = kwargs

            async def gen():
                for e in events:
                    yield e
            return gen()

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = llm_openai.OpenAILLM("x", client=client)
    chunks = [c async for c in llm.stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])]
    assert chunks[0].text == "Let me check. "
    assert chunks[-1].tool_calls[0].name == "check_availability"
    assert chunks[-1].tool_calls[0].arguments == {"date": "2026-10-19"}
    # Gemini 3 thought signature is preserved so it can be echoed back
    assert chunks[-1].tool_calls[0].extra == {"extra_content": {"google": {"thought_signature": "SIG=="}}}
    assert completions.kwargs["stream"] is True and completions.kwargs["tools"]


async def test_llm_retries_after_rate_limit():
    import httpx
    from openai import RateLimitError

    attempts = []

    class Completions:
        async def create(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
                raise RateLimitError("Quota exceeded. Please retry in 0.1s.", response=resp, body=None)

            async def gen():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))])
            return gen()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    llm = llm_openai.OpenAILLM("x", client=client, max_retry_wait_s=5)
    chunks = [c async for c in llm.stream([{"role": "user", "content": "hi"}], tools=[])]
    assert len(attempts) == 2 and chunks[0].text == "ok"


async def test_reasoning_effort_is_sent_and_dropped_if_unsupported():
    import httpx
    from openai import BadRequestError

    seen = []

    class Completions:
        async def create(self, **kwargs):
            seen.append(kwargs.get("reasoning_effort"))
            if "reasoning_effort" in kwargs:
                resp = httpx.Response(400, request=httpx.Request("POST", "https://x"))
                raise BadRequestError("reasoning_effort is not supported by this model", response=resp, body=None)

            async def gen():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))])
            return gen()

    llm = llm_openai.OpenAILLM("x", client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
                               reasoning_effort="minimal")
    chunks = [c async for c in llm.stream([{"role": "user", "content": "hi"}], tools=[])]
    assert seen == ["minimal", None] and chunks[0].text == "ok"
    assert llm.reasoning_effort is None  # not retried on every later request


def test_retry_after_understands_long_waits():
    from app.providers.llm_openai import _retry_after

    assert _retry_after(Exception("Please retry in 32.5s.")) == 32.5
    assert _retry_after(Exception("Please retry in 6h44m20.5s.")) == 6 * 3600 + 44 * 60 + 20.5
    assert _retry_after(Exception("retry in 2m")) == 120
    assert _retry_after(Exception("try later")) is None
