"""Application container: wires settings, database, providers and tools together."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .business import Business, load_business
from .config import Settings
from .db import Database
from .knowledge import KnowledgeStore, OpenAIEmbedder
from .pipeline.session import CallSession, SessionDeps
from .providers import build_llm, build_stt, build_tts
from .providers.base import LLMProvider, STTProvider, TTSProvider
from .tools.calendar import GoogleCalendarSync, LocalCalendar
from .tools.registry import ToolContext, ToolExecutor
from .tools.twilio_client import TwilioClient

log = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    business: Business
    db: Database
    stt: STTProvider
    llm: LLMProvider
    tts: TTSProvider
    calendar: LocalCalendar
    twilio: TwilioClient
    tools: ToolExecutor
    knowledge: KnowledgeStore
    active_sessions: dict[str, CallSession] = field(default_factory=dict)
    uses_custom_providers: bool = False  # True when providers were injected (tests / evals)

    @classmethod
    def build(cls, settings: Settings, *, stt: STTProvider | None = None, llm: LLMProvider | None = None,
              tts: TTSProvider | None = None) -> AppState:
        business = load_business(settings.business_config_path)
        db = Database(settings.database_url)
        if settings.google_calendar_enabled:
            info = json.loads(settings.google_service_account_json) if settings.google_service_account_json else None
            calendar: LocalCalendar = GoogleCalendarSync(
                db, business, settings.google_calendar_id,
                service_account_file=settings.google_service_account_file, service_account_info=info)
            log.info("Google Calendar sync enabled (%s)", settings.google_calendar_id)
        else:
            calendar = LocalCalendar(db, business)
        twilio = TwilioClient(settings)
        model = settings.resolved_embedding_model
        embedder = (OpenAIEmbedder(settings.openai_api_key, model, base_url=settings.openai_base_url or None)
                    if model and llm is None else None)
        knowledge = KnowledgeStore(db, embedder, top_k=settings.rag_top_k)
        return cls(
            settings=settings, business=business, db=db,
            stt=stt or build_stt(settings), llm=llm or build_llm(settings), tts=tts or build_tts(settings),
            calendar=calendar, twilio=twilio, tools=ToolExecutor(business, calendar, twilio, knowledge),
            knowledge=knowledge,
            uses_custom_providers=any(p is not None for p in (stt, llm, tts)),
        )

    async def new_session(self, transport, *, channel: str, caller: str | None = None,
                          external_id: str | None = None, language: str | None = None) -> CallSession:
        call = await self.db.create_call(channel=channel, caller=caller, external_id=external_id)
        ctx = ToolContext(call_id=call.id, channel=channel, caller_phone=caller, external_call_id=external_id,
                          language=language or (self.business.languages[0] if self.business.languages else "en-IN"))
        deps = SessionDeps(business=self.business, db=self.db, stt=self.stt, llm=self.llm, tts=self.tts,
                           tools=self.tools, end_of_turn_delay_ms=self.settings.end_of_turn_delay_ms,
                           incomplete_turn_delay_ms=self.settings.incomplete_turn_delay_ms,
                           max_call_seconds=self.settings.max_call_seconds)
        try:
            session = CallSession(deps, transport, ctx)
        except Exception:
            await self.db.finish_call(call.id, status="failed", outcome="failed")
            raise
        self.active_sessions[call.id] = session
        return session

    def release(self, session: CallSession) -> None:
        self.active_sessions.pop(session.ctx.call_id, None)
