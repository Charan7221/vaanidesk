"""Database models and data-access helpers (SQLAlchemy 2.0 async).

SQLite is used by default; set DATABASE_URL=postgresql+asyncpg://... for Postgres.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, with_loader_criteria


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


# ------------------------------------------------------------------ multi-clinic (tenancy)
DEFAULT_CLINIC = "default"
#: The clinic the current request / call belongs to. Every query on a ClinicScoped table is filtered by it,
#: and every new row is stamped with it, so one clinic can never see another clinic's data.
current_clinic: ContextVar[str] = ContextVar("current_clinic", default=DEFAULT_CLINIC)


def _current_clinic() -> str:
    return current_clinic.get()


class ClinicScoped:
    clinic_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, default=_current_clinic)


@event.listens_for(Session, "do_orm_execute")
def _scope_to_clinic(state) -> None:
    if state.execution_options.get("all_clinics") or not (state.is_select or state.is_update or state.is_delete):
        return
    clinic = current_clinic.get()
    state.statement = state.statement.options(
        with_loader_criteria(ClinicScoped, lambda cls: cls.clinic_id == clinic, include_aliases=True))


class Clinic(Base):
    """A customer (clinic or hospital). ``config`` is the business profile (same fields as business.yaml)."""

    __tablename__ = "clinics"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # slug, used in links: /demo?clinic=<id>
    name: Mapped[str] = mapped_column(String(160))
    config: Mapped[dict] = mapped_column(JSON)
    config_file: Mapped[str | None] = mapped_column(String(255), nullable=True)  # re-read at startup if set
    phone_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)  # inbound numbers routed here
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(ClinicScoped, Base):
    """A staff login. role: owner (clinic admin: setup + staff) | staff (reception) | superadmin (all clinics)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="staff")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(ClinicScoped, Base):
    """Who did what: confirmations, cancellations, setup changes, staff changes, logins."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(160), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Call(ClinicScoped, Base):
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


class Appointment(ClinicScoped, Base):
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
    patient_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fee_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # new | follow_up
    fee_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Pilot mode: AI bookings wait for staff (False = pending). None/True = confirmed.
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# One active booking per doctor per start time, enforced by the database itself (two simultaneous calls
# can't both win the same slot, even across server processes).
UNIQUE_SLOT = Index("uq_appointment_slot", Appointment.provider_id, Appointment.start, unique=True,
                    sqlite_where=text("status = 'booked'"), postgresql_where=text("status = 'booked'"))


class FollowUp(ClinicScoped, Base):
    """A patient request that a person must finish: failed call, outage, abandoned booking, callback request,
    or an AI booking that staff rejected in pilot mode. Shown on the dashboard until someone marks it done."""

    __tablename__ = "follow_ups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    call_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24))  # outage | abandoned | callback | transfer_failed | rejected
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    patient_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeChunk(ClinicScoped, Base):
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


class Block(ClinicScoped, Base):
    """Time when a provider (or the whole clinic, provider_id=None) is unavailable."""

    __tablename__ = "blocks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Patient(ClinicScoped, Base):
    """A registered OPD patient. Family members can share a phone, so a patient is (phone, name)."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    number: Mapped[int] = mapped_column(Integer, unique=True)  # patient number spoken to the caller, e.g. 1001
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OpdToken(ClinicScoped, Base):
    """A walk-in OPD token: number N in one doctor's queue for one session (day + session start)."""

    __tablename__ = "opd_tokens"
    __table_args__ = (UniqueConstraint("clinic_id", "day", "provider_id", "session", "number", name="uq_opd_token_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    day: Mapped[str] = mapped_column(String(10), index=True)  # local date, YYYY-MM-DD
    session: Mapped[str] = mapped_column(String(5))  # session start, HH:MM
    provider_id: Mapped[str] = mapped_column(String(64))
    number: Mapped[int] = mapped_column(Integer)
    patient_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    patient_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="call")  # call | staff
    call_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # waiting -> in_consultation -> done ; or skipped (missed their turn) / cancelled
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    fee_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # new | follow_up
    fee_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # pilot mode, as for appointments
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpdSessionStatus(ClinicScoped, Base):
    """Staff notes on one doctor's OPD session: running late, or cancelled for the day."""

    __tablename__ = "opd_session_status"
    __table_args__ = (UniqueConstraint("clinic_id", "day", "provider_id", "session", name="uq_opd_session"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    day: Mapped[str] = mapped_column(String(10), index=True)
    session: Mapped[str] = mapped_column(String(5))
    provider_id: Mapped[str] = mapped_column(String(64))
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def _add_missing_columns(sync_conn) -> None:
    """Tiny migration: add new nullable columns to tables created by an older version (create_all only
    creates missing tables). Keeps existing SQLite/Postgres databases working after an upgrade."""
    insp = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have and col.nullable:
                ddl = col.type.compile(dialect=sync_conn.dialect)
                sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))
                log.info("Added column %s.%s", table.name, col.name)


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, future=True)
        if self.engine.dialect.name == "sqlite":
            # WAL lets the dashboard read while a call is writing (no "database is locked"), and a busy
            # timeout makes concurrent writers wait instead of failing.
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.close()
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
            await conn.run_sync(_add_missing_columns)
            # rows from before multi-clinic support belong to the first (default) clinic
            for table in Base.metadata.sorted_tables:
                if "clinic_id" in table.c:
                    await conn.execute(table.update().where(table.c.clinic_id.is_(None))
                                       .values(clinic_id=DEFAULT_CLINIC))
        try:  # databases created before the index existed: add it (fails only if duplicates already exist)
            async with self.engine.begin() as conn:
                await conn.run_sync(lambda c: UNIQUE_SLOT.create(c, checkfirst=True))
        except Exception as exc:
            log.warning("Could not add the one-booking-per-slot index (duplicate bookings exist?): %s", exc)

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

    # ---------- follow-ups ----------
    async def add_follow_up(self, kind: str, reason: str, call_id: str | None = None, phone: str | None = None,
                            patient_name: str | None = None) -> FollowUp | None:
        """Add a follow-up, at most one open one per call and kind (so retries don't flood the list)."""
        async with self.session() as s:
            if call_id:
                dup = (await s.execute(select(FollowUp).where(FollowUp.call_id == call_id, FollowUp.kind == kind,
                                                              FollowUp.status == "open"))).scalars().first()
                if dup:
                    return None
            item = FollowUp(kind=kind, reason=reason[:2000], call_id=call_id, phone=phone, patient_name=patient_name)
            s.add(item)
            await s.commit()
            return item

    async def follow_ups(self, status: str | None = "open", limit: int = 200) -> list[FollowUp]:
        async with self.session() as s:
            q = select(FollowUp)
            if status:
                q = q.where(FollowUp.status == status)
            return list((await s.execute(q.order_by(FollowUp.created_at.desc()).limit(limit))).scalars())

    async def resolve_follow_up(self, item_id: str, note: str | None = None) -> FollowUp | None:
        async with self.session() as s:
            item = await s.get(FollowUp, item_id)
            if item:
                item.status, item.resolved_at, item.note = "done", utcnow(), (note or "").strip() or None
                await s.commit()
            return item

    # ---------- privacy ----------
    async def purge_transcripts(self, older_than_days: int) -> int:
        """Delete call transcripts (turns) and summaries older than the retention period. Call metadata
        (time, outcome, latency) stays for the dashboard statistics. Returns the number of calls purged."""
        if older_than_days <= 0:
            return 0
        cutoff = utcnow() - timedelta(days=older_than_days)
        async with self.session() as s:
            old_ids = list((await s.execute(select(Call.id).where(Call.started_at < cutoff))).scalars())
            if not old_ids:
                return 0
            await s.execute(Turn.__table__.delete().where(Turn.call_id.in_(old_ids)))
            await s.execute(Call.__table__.update().where(Call.id.in_(old_ids)).values(summary=None))
            await s.commit()
            return len(old_ids)
