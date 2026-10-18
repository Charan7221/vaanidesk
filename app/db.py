"""Database models and data-access helpers (SQLAlchemy 2.0 async).

SQLite is used by default; set DATABASE_URL=postgresql+asyncpg://... for Postgres.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """Normalise to tz-aware UTC (SQLite returns naive datetimes; we always store UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    channel: Mapped[str] = mapped_column(String(16))  # "phone" | "browser"
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Twilio CallSid
    caller: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|completed|transferred|failed
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)  # booked|cancelled|info|transferred
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    turns_count: Mapped[int] = mapped_column(Integer, default=0)
    interruptions: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    turns: Mapped[list[Turn]] = relationship(back_populates="call", order_by="Turn.id", cascade="all, delete-orphan")


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    call_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    patient_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="booked")  # booked|cancelled
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    """A searchable piece of a clinic document (RAG). Embeddings are searched with pgvector on Postgres."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(255), index=True)  # file name, used as the citation
    origin: Mapped[str] = mapped_column(String(16), default="folder")  # folder | upload
    doc_hash: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Block(Base):
    """Time when a provider (or the whole clinic, provider_id=None) is unavailable."""

    __tablename__ = "blocks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, future=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    #: True when the database is Postgres with the pgvector extension (vector search runs in SQL)
    pgvector: bool = False

    async def create_all(self) -> None:
        if self.engine.dialect.name == "postgresql":
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                self.pgvector = True
            except Exception as exc:  # no permission / not installed: fall back to in-app similarity
                log.warning("pgvector not available (%s); using in-app vector search", exc)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    # ---------- calls ----------
    async def create_call(self, channel: str, caller: str | None = None, external_id: str | None = None) -> Call:
        async with self.session() as s:
            call = Call(channel=channel, caller=caller, external_id=external_id)
            s.add(call)
            await s.commit()
            return call

    async def add_turn(self, call_id: str, role: str, text: str, language: str | None = None,
                       latency_ms: float | None = None, meta: dict | None = None) -> None:
        async with self.session() as s:
            s.add(Turn(call_id=call_id, role=role, text=text, language=language, latency_ms=latency_ms, meta=meta))
            await s.commit()

    async def finish_call(self, call_id: str, **fields) -> None:
        async with self.session() as s:
            call = await s.get(Call, call_id)
            if not call:
                return
            for k, v in fields.items():
                setattr(call, k, v)
            if call.ended_at is None:
                call.ended_at = utcnow()
            await s.commit()

    async def update_call(self, call_id: str, **fields) -> None:
        async with self.session() as s:
            call = await s.get(Call, call_id)
            if call:
                for k, v in fields.items():
                    setattr(call, k, v)
                await s.commit()

    # ---------- appointments ----------
    async def appointments_between(self, start: datetime, end: datetime, provider_id: str | None = None) -> list[Appointment]:
        start, end = as_utc(start), as_utc(end)  # SQLite stores naive UTC: always compare in UTC
        async with self.session() as s:
            q = select(Appointment).where(Appointment.status == "booked", Appointment.start < end, Appointment.end > start)
            if provider_id:
                q = q.where(Appointment.provider_id == provider_id)
            return list((await s.execute(q.order_by(Appointment.start))).scalars())

    async def add_appointment(self, **fields) -> Appointment:
        for key in ("start", "end"):
            if key in fields:
                fields[key] = as_utc(fields[key])
        async with self.session() as s:
            appt = Appointment(**fields)
            s.add(appt)
            await s.commit()
            return appt

    async def find_appointments(self, phone: str | None = None, name: str | None = None,
                                after: datetime | None = None) -> list[Appointment]:
        async with self.session() as s:
            q = select(Appointment).where(Appointment.status == "booked")
            if phone:
                q = q.where(Appointment.phone == phone)
            if name:
                q = q.where(func.lower(Appointment.patient_name).contains(name.lower()))
            if after:
                q = q.where(Appointment.start >= as_utc(after))
            return list((await s.execute(q.order_by(Appointment.start))).scalars())

    # ---------- blocked time ----------
    async def blocks_between(self, start: datetime, end: datetime, provider_id: str | None = None,
                             include_all_providers: bool = False) -> list[Block]:
        """Blocks overlapping [start, end). provider_id given -> that provider's blocks + clinic-wide blocks."""
        start, end = as_utc(start), as_utc(end)
        async with self.session() as s:
            q = select(Block).where(Block.start < end, Block.end > start)
            if provider_id and not include_all_providers:
                q = q.where((Block.provider_id == provider_id) | (Block.provider_id.is_(None)))
            return list((await s.execute(q.order_by(Block.start))).scalars())

    async def add_block(self, start: datetime, end: datetime, provider_id: str | None = None,
                        reason: str | None = None) -> Block:
        async with self.session() as s:
            block = Block(start=as_utc(start), end=as_utc(end), provider_id=provider_id, reason=reason)
            s.add(block)
            await s.commit()
            return block

    async def delete_block(self, block_id: str) -> bool:
        async with self.session() as s:
            block = await s.get(Block, block_id)
            if not block:
                return False
            await s.delete(block)
            await s.commit()
            return True

    async def get_appointment(self, appt_id: str) -> Appointment | None:
        async with self.session() as s:
            return await s.get(Appointment, appt_id)

    async def update_appointment(self, appt_id: str, **fields) -> None:
        async with self.session() as s:
            appt = await s.get(Appointment, appt_id)
            if appt:
                for k, v in fields.items():
                    setattr(appt, k, v)
                await s.commit()

    async def set_external_event_id(self, appt_id: str, event_id: str | None) -> None:
        async with self.session() as s:
            appt = await s.get(Appointment, appt_id)
            if appt:
                appt.external_event_id = event_id
                await s.commit()

    async def cancel_appointment(self, appt_id: str) -> Appointment | None:
        async with self.session() as s:
            appt = await s.get(Appointment, appt_id)
            if appt and appt.status == "booked":
                appt.status = "cancelled"
                await s.commit()
            return appt
