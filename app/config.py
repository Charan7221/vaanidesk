"""Application settings, loaded from environment variables / .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    app_env: str = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"  # e.g. https://abc.ngrok.app (used for Twilio webhooks)
    business_config_path: str = str(ROOT_DIR / "config" / "business.yaml")
    database_url: str = "sqlite+aiosqlite:///./vaanidesk.db"

    # --- Dashboard auth (HTTP Basic) ---
    admin_user: str = "admin"
    admin_password: str = "change-me"

    # --- LLM (OpenAI) ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    # Any OpenAI-compatible API works: leave empty for OpenAI, or e.g.
    #   Gemini: https://generativelanguage.googleapis.com/v1beta/openai/
    #   Groq:   https://api.groq.com/openai/v1
    openai_base_url: str = ""
    llm_temperature: float = 0.4
    llm_max_retry_wait_s: float = 6.0  # on rate limits (429), wait at most this long per retry
    # How much the model "thinks" before answering. Less = faster replies. Empty = provider default.
    # Gemini 3: minimal | low | medium | high.  OpenAI reasoning models: minimal | low | medium | high.
    llm_reasoning_effort: str = ""

    # --- Knowledge base (RAG over clinic documents) ---
    knowledge_dir: str = str(ROOT_DIR / "knowledge")
    # Embedding model on the same OpenAI-compatible API. "auto" picks one for the provider
    # (Gemini: gemini-embedding-001, OpenAI: text-embedding-3-small); "off" = keyword search only.
    embedding_model: str = "auto"
    rag_top_k: int = 3

    # --- Speech-to-text ---
    stt_provider: str = "sarvam"  # "sarvam" (auto-detects en/hi/te) or "deepgram"
    sarvam_api_key: str = ""
    sarvam_stt_model: str = "saaras:v4"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    deepgram_language: str = "multi"  # "multi" = code-switching; or "en", "hi", "te"

    # --- Text-to-speech (Sarvam Bulbul) ---
    tts_mode: str = "stream"  # "stream" (WebSocket) or "rest" (one request per sentence)
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_speaker: str = "priya"
    sarvam_tts_pace: float = 1.05

    # --- Twilio ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""  # E.164, e.g. +14155550123
    twilio_whatsapp_from: str = ""  # e.g. whatsapp:+14155238886 (optional)
    twilio_validate_signature: bool = True

    # --- Google Calendar (optional; local calendar is used if unset) ---
    google_service_account_file: str = ""
    google_service_account_json: str = ""  # alternative for cloud hosts: paste the whole JSON key here
    google_calendar_id: str = ""
    # Secret part of the calendar subscription link (derived from ADMIN_PASSWORD if empty)
    calendar_feed_token: str = ""

    # --- Browser demo protection (stops strangers burning your API credits) ---
    demo_access_code: str = ""  # if set, the web demo asks for this code
    max_concurrent_calls: int = 5

    # --- Conversation tuning ---
    end_of_turn_delay_ms: int = 350  # wait after final transcript before replying
    incomplete_turn_delay_ms: int = 1200  # ...longer if the caller stopped mid-sentence
    max_call_seconds: int = 600

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        # Render/Railway give postgres:// URLs; SQLAlchemy async needs postgresql+asyncpg://
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @model_validator(mode="after")
    def _hosted_url(self) -> Settings:
        # On Render the public URL is provided automatically.
        render_url = os.environ.get("RENDER_EXTERNAL_URL")
        if render_url and "localhost" in self.public_base_url:
            self.public_base_url = render_url
        return self

    @property
    def google_calendar_enabled(self) -> bool:
        return bool(self.google_calendar_id and (self.google_service_account_file or self.google_service_account_json))

    @property
    def resolved_embedding_model(self) -> str:
        from .knowledge.embeddings import default_embedding_model

        if self.embedding_model.lower() in ("off", "none", ""):
            return ""
        if self.embedding_model.lower() == "auto":
            return default_embedding_model(self.openai_base_url) if self.openai_api_key else ""
        return self.embedding_model

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def missing_keys(self) -> list[str]:
        """API keys required for live calls that are not configured."""
        missing = []
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if not self.sarvam_api_key:
            missing.append("SARVAM_API_KEY")
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        return missing

    @property
    def public_ws_base(self) -> str:
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
