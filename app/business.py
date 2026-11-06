"""Business profile: opening hours, providers, FAQ, policies (loaded from YAML)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Provider(BaseModel):
    id: str
    name: str
    specialty: str = ""
    department: str | None = None  # department id
    consultation_fee: int | None = None  # overrides the department's fee (e.g. a senior consultant)


class Department(BaseModel):
    """A clinical department. ``treats`` lists everyday problems (in English) that belong here, so the
    receptionist can send a new patient to the right doctor from what they describe."""

    id: str
    name: str
    treats: list[str] = Field(default_factory=list)
    age_min: int | None = None  # e.g. 15 for adult medicine
    age_max: int | None = None  # e.g. 14 for paediatrics
    gender: str | None = None  # "female" for gynaecology / obstetrics
    default: bool = False  # where patients go when the problem doesn't match any department
    consultation_fee: int | None = None
    follow_up_days: int | None = None
    follow_up_fee: int | None = None


class FeePolicy(BaseModel):
    """Clinic-wide defaults; departments and doctors can override them."""

    currency: str = "₹"
    consultation_fee: int = 0
    follow_up_days: int = 7  # a repeat visit to the same department within this many days ...
    follow_up_fee: int = 0  # ... costs this much (0 = free follow-up)


class Service(BaseModel):
    name: str
    price: str = ""


class FAQ(BaseModel):
    q: str
    a: str


class OPDConfig(BaseModel):
    """Walk-in OPD (outpatient) token queue. Each opening window of a day is one OPD session."""

    enabled: bool = False
    avg_consult_minutes: int = 10  # used to estimate waiting time
    max_tokens_per_session: int = 40  # per doctor
    stop_before_end_minutes: int = 30  # no new tokens this close to the end of a session
    advance_days: int = 2  # tokens can be taken for today and up to this many days ahead


class ReminderConfig(BaseModel):
    """Day-before reminders (WhatsApp, else SMS) for appointments and OPD tokens: fewer no-shows."""

    enabled: bool = True
    send_at: str = "18:00"  # local time, the evening before the visit


class Business(BaseModel):
    name: str
    assistant_name: str = "Vaani"
    timezone: str = "Asia/Kolkata"
    address: str = ""
    phone: str = ""
    languages: list[str] = Field(default_factory=lambda: ["en-IN"])
    # "full": same greeting in every language | "short": one greeting + "you can also speak X" | "off"
    multilingual_greeting: str = "full"
    emergency_number: str = "108"  # ambulance number callers are told to dial in an emergency

    @field_validator("multilingual_greeting", mode="before")
    @classmethod
    def _greeting_mode(cls, v):
        if v is True:
            return "full"
        if v in (False, None):
            return "off"
        return str(v).lower()
    slot_minutes: int = 20
    booking_horizon_days: int = 14
    hours: dict[str, list[list[str]]] = Field(default_factory=dict)
    providers: list[Provider] = Field(default_factory=list)
    services: list[Service] = Field(default_factory=list)
    faq: list[FAQ] = Field(default_factory=list)
    handoff_number: str = ""
    policies: list[str] = Field(default_factory=list)
    opd: OPDConfig = Field(default_factory=OPDConfig)
    # Supervised pilot: bookings and tokens made by the AI stay "pending" until staff confirm them
    # on the dashboard; the caller is told the clinic will confirm by SMS.
    pilot_mode: bool = False
    # Tell callers at the start that they're speaking to an AI assistant and the call is transcribed.
    consent_notice: bool = True
    transcript_retention_days: int = 90  # call transcripts and summaries are deleted after this (0 = keep)
    reminders: ReminderConfig = Field(default_factory=ReminderConfig)
    staff_alert_phone: str = ""  # SMS/WhatsApp for new follow-ups (defaults to handoff_number)
    departments: list[Department] = Field(default_factory=list)
    fees: FeePolicy = Field(default_factory=FeePolicy)
    # Words that may mean an emergency: the agent checks before routing to a normal OPD doctor.
    red_flags: list[str] = Field(default_factory=lambda: [
        "chest pain", "can't breathe", "cannot breathe", "breathing difficulty", "unconscious", "fainted",
        "seizure", "fits", "heavy bleeding", "severe bleeding", "stroke", "face drooping", "slurred speech",
        "accident", "poison", "snake bite", "suicide", "severe burn"])

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def provider(self, provider_id: str | None) -> Provider | None:
        """Find a provider by id or by (part of) the name: "dr-rao", "Dr. Rao", "Doctor Rao", "lakshmi"."""
        if not provider_id:
            return self.providers[0] if self.providers else None
        key = provider_id.lower().strip()
        for p in self.providers:
            if p.id.lower() == key:
                return p
        words = [w for w in re.split(r"[^\w]+", key) if w and w not in {"dr", "doctor", "doc", "garu", "ji"}]
        if not words:
            return None
        for p in self.providers:
            haystack = re.split(r"[^\w]+", (p.name + " " + p.id).lower())
            if all(w in haystack for w in words):
                return p
        return None

    def department(self, department_id: str | None) -> Department | None:
        return next((d for d in self.departments if d.id == department_id), None) if department_id else None

    def department_of(self, provider_id: str | None) -> Department | None:
        p = self.provider(provider_id) if provider_id else None
        return self.department(p.department) if p else None

    def providers_in(self, department_id: str) -> list[Provider]:
        return [p for p in self.providers if p.department == department_id]

    def fee_rules(self, provider_id: str | None) -> tuple[int, int, int]:
        """(consultation fee, follow-up window in days, follow-up fee) for a doctor."""
        p = self.provider(provider_id) if provider_id else None
        d = self.department_of(provider_id)
        f = self.fees
        fee = next((v for v in (p.consultation_fee if p else None, d.consultation_fee if d else None)
                    if v is not None), f.consultation_fee)
        days = d.follow_up_days if d and d.follow_up_days is not None else f.follow_up_days
        fu = d.follow_up_fee if d and d.follow_up_fee is not None else f.follow_up_fee
        return fee, days, fu

    def opening_windows(self, day: date) -> list[tuple[datetime, datetime]]:
        """Return the open time windows for a calendar day, as tz-aware datetimes."""
        windows = []
        for start_s, end_s in self.hours.get(WEEKDAYS[day.weekday()], []) or []:
            start = datetime.combine(day, time.fromisoformat(start_s), self.tz)
            end = datetime.combine(day, time.fromisoformat(end_s), self.tz)
            windows.append((start, end))
        return windows

    def candidate_slots(self, day: date) -> list[datetime]:
        """All slot start times for a day according to opening hours (ignores bookings)."""
        step = timedelta(minutes=self.slot_minutes)
        slots = []
        for start, end in self.opening_windows(day):
            t = start
            while t + step <= end:
                slots.append(t)
                t += step
        return slots

    def opd_sessions(self, day: date) -> list[OPDSession]:
        """OPD sessions for a day: one per opening window (e.g. morning 09:00-13:00, evening 17:00-20:00)."""
        return [OPDSession(key=start.strftime("%H:%M"), label=session_label(start), start=start, end=end)
                for start, end in self.opening_windows(day)]

    def hours_summary(self) -> str:
        parts = []
        for d in WEEKDAYS:
            win = self.hours.get(d) or []
            label = d.capitalize()
            parts.append(f"{label}: " + (", ".join(f"{a}-{b}" for a, b in win) if win else "closed"))
        return "; ".join(parts)


class OPDSession(BaseModel):
    key: str  # start time "HH:MM", stored with each token
    label: str  # morning | afternoon | evening
    start: datetime
    end: datetime


def session_label(start: datetime) -> str:
    return "morning" if start.hour < 12 else "afternoon" if start.hour < 16 else "evening"


def load_business(path: str) -> Business:
    with open(path, encoding="utf-8") as f:
        return Business.model_validate(yaml.safe_load(f))


@lru_cache
def get_business(path: str) -> Business:
    return load_business(path)
