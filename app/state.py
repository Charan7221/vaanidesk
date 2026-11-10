"""Application container: wires settings, database, providers and tools together.

One server serves many clinics. Shared: settings, database, AI providers, Twilio. Per clinic (a ``Tenant``):
business profile, calendar, agent tools and knowledge base. ``state.business`` / ``state.tools`` / ... resolve
to the clinic of the current request or call (see ``app.db.current_clinic``), so most code doesn't need to
know about clinics at all.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select

from .business import Business, load_business
from .config import Settings
from .db import DEFAULT_CLINIC, Clinic, Database, current_clinic
from .knowledge import KnowledgeStore, OpenAIEmbedder
from .messaging import Notifier
from .pipeline.session import CallSession, SessionDeps
from .providers import build_llm, build_stt, build_tts
from .providers.base import LLMProvider, STTProvider, TTSProvider
from .tools.calendar import GoogleCalendarSync, LocalCalendar
from .tools.registry import ToolContext, ToolExecutor
from .tools.twilio_client import TwilioClient

log = logging.getLogger(__name__)


class UnknownClinic(LookupError):
    pass


@dataclass
class Tenant:
    clinic_id: str
    business: Business
    calendar: LocalCalendar
    tools: ToolExecutor
    knowledge: KnowledgeStore


class AppState:
    def __init__(self, settings: Settings, db: Database, stt: STTProvider, llm: LLMProvider, tts: TTSProvider,
                 twilio: TwilioClient, embedder, uses_custom_providers: bool = False):
        self.settings, self.db, self.stt, self.llm, self.tts = settings, db, stt, llm, tts
        self.twilio, self.embedder = twilio, embedder
        self.notifier = Notifier(settings, twilio, db)  # WhatsApp / SMS / email to patients and staff
        self.uses_custom_providers = uses_custom_providers  # True when providers were injected (tests / evals)
        self.tenants: dict[str, Tenant] = {}
        self.active_sessions: dict[str, CallSession] = {}

    @classmethod
    def build(cls, settings: Settings, *, stt: STTProvider | None = None, llm: LLMProvider | None = None,
              tts: TTSProvider | None = None) -> AppState:
        db = Database(settings.database_url)
        model = settings.resolved_embedding_model
        embedder = (OpenAIEmbedder(settings.openai_api_key, model, base_url=settings.openai_base_url or None)
                    if model and llm is None else None)
        state = cls(settings, db, stt or build_stt(settings), llm or build_llm(settings), tts or build_tts(settings),
                    TwilioClient(settings), embedder, uses_custom_providers=any(p is not None for p in (stt, llm, tts)))
        # The first clinic comes from business.yaml, so a single-clinic setup works exactly as before.
        state.add_tenant(DEFAULT_CLINIC, load_business(settings.business_config_path))
        return state

    # ------------------------------------------------------------------ clinics
    def add_tenant(self, clinic_id: str, business: Business) -> Tenant:
        if clinic_id == DEFAULT_CLINIC and self.settings.google_calendar_enabled:
            info = (json.loads(self.settings.google_service_account_json)
                    if self.settings.google_service_account_json else None)
            calendar: LocalCalendar = GoogleCalendarSync(
                self.db, business, self.settings.google_calendar_id,
                service_account_file=self.settings.google_service_account_file, service_account_info=info)
            log.info("Google Calendar sync enabled (%s)", self.settings.google_calendar_id)
        else:
            calendar = LocalCalendar(self.db, business)
        knowledge = KnowledgeStore(self.db, self.embedder, top_k=self.settings.rag_top_k)
        tenant = Tenant(clinic_id, business, calendar,
                        ToolExecutor(business, calendar, self.twilio, knowledge, notifier=self.notifier), knowledge)
        self.tenants[clinic_id] = tenant
        return tenant

    def tenant(self, clinic_id: str | None = None) -> Tenant:
        cid = clinic_id or current_clinic.get()
        try:
            return self.tenants[cid]
        except KeyError:
            raise UnknownClinic(cid) from None

    @property
    def clinic_id(self) -> str:
        return current_clinic.get()

    @property
    def business(self) -> Business:
        return self.tenant().business

    @property
    def calendar(self) -> LocalCalendar:
        return self.tenant().calendar

    @property
    def tools(self) -> ToolExecutor:
        return self.tenant().tools

    @property
    def knowledge(self) -> KnowledgeStore:
        return self.tenant().knowledge

    async def load_clinics(self) -> None:
        """Startup: create the first clinic from business.yaml if the database has none, then load every
        active clinic. Clinics linked to a config file (the first one, by default) re-read it at startup."""
        async with self.db.session() as s:
            clinics = list((await s.execute(select(Clinic))).scalars())
            if not clinics:
                biz = self.tenants[DEFAULT_CLINIC].business
                s.add(Clinic(id=DEFAULT_CLINIC, name=biz.name, config=biz.model_dump(mode="json"),
                             config_file=self.settings.business_config_path))
                await s.commit()
                return
            for c in clinics:
                if not c.active:
                    self.tenants.pop(c.id, None)
                    continue
                if c.config_file and Path(c.config_file).exists():
                    with open(c.config_file, encoding="utf-8") as f:
                        fresh = Business.model_validate(yaml.safe_load(f))
                    if c.config != fresh.model_dump(mode="json"):
                        c.config, c.name = fresh.model_dump(mode="json"), fresh.name
                    if c.id in self.tenants:  # already built from the same file
                        continue
                    self.add_tenant(c.id, fresh)
                else:
                    self.add_tenant(c.id, Business.model_validate(c.config))
            await s.commit()

    async def save_clinic_config(self, clinic_id: str, business: Business) -> None:
        """Setup page: store a clinic's new profile and use it for new calls immediately."""
        async with self.db.session() as s:
            c = await s.get(Clinic, clinic_id)
            if c is None:
                raise UnknownClinic(clinic_id)
            c.config, c.name, c.config_file = business.model_dump(mode="json"), business.name, None
            await s.commit()
        self.add_tenant(clinic_id, business)

    async def clinic_for_number(self, number: str | None) -> str:
        """Which clinic an incoming phone number (Twilio "To" / Exotel "To") belongs to."""
        if number:
            async with self.db.session() as s:
                for c in (await s.execute(select(Clinic).where(Clinic.active.is_(True)))).scalars():
                    if number in (c.phone_numbers or []):
                        return c.id
        return DEFAULT_CLINIC

    # ------------------------------------------------------------------ calls
    async def new_session(self, transport, *, channel: str, caller: str | None = None,
                          external_id: str | None = None, language: str | None = None) -> CallSession:
        t = self.tenant()
        call = await self.db.create_call(channel=channel, caller=caller, external_id=external_id)
        ctx = ToolContext(call_id=call.id, channel=channel, caller_phone=caller, external_call_id=external_id,
                          language=language or (t.business.languages[0] if t.business.languages else "en-IN"))
        deps = SessionDeps(business=t.business, db=self.db, stt=self.stt, llm=self.llm, tts=self.tts,
                           tools=t.tools, end_of_turn_delay_ms=self.settings.end_of_turn_delay_ms,
                           incomplete_turn_delay_ms=self.settings.incomplete_turn_delay_ms,
                           max_call_seconds=self.settings.max_call_seconds,
                           show_errors=not self.settings.is_production)
        try:
            session = CallSession(deps, transport, ctx)
        except Exception:
            await self.db.finish_call(call.id, status="failed", outcome="failed")
            raise
        self.active_sessions[call.id] = session
        return session

    def release(self, session: CallSession) -> None:
        self.active_sessions.pop(session.ctx.call_id, None)
