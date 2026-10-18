"""Provider factory: builds STT / LLM / TTS from settings."""
from __future__ import annotations

from ..config import Settings
from .base import LLMProvider, STTProvider, TTSProvider


def build_stt(s: Settings) -> STTProvider:
    if s.stt_provider == "deepgram":
        from .stt_deepgram import DeepgramSTT

        return DeepgramSTT(s.deepgram_api_key, s.deepgram_model, s.deepgram_language)
    from .stt_sarvam import SarvamSTT

    return SarvamSTT(s.sarvam_api_key, s.sarvam_stt_model)


def build_llm(s: Settings) -> LLMProvider:
    from .llm_openai import OpenAILLM

    # A placeholder lets the server (dashboard, docs) start without a key; calls report the missing key.
    return OpenAILLM(s.openai_api_key or "missing-key", s.openai_model, s.llm_temperature,
                     base_url=s.openai_base_url or None, max_retry_wait_s=s.llm_max_retry_wait_s,
                     reasoning_effort=s.llm_reasoning_effort or None)


def build_tts(s: Settings) -> TTSProvider:
    from .tts_sarvam import SarvamTTS

    return SarvamTTS(s.sarvam_api_key, s.sarvam_tts_model, s.sarvam_tts_speaker, s.sarvam_tts_pace, s.tts_mode)
