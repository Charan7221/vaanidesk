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


class Service(BaseModel):
    name: str
    price: str = ""


class FAQ(BaseModel):
    q: str
    a: str


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

    def hours_summary(self) -> str:
        parts = []
        for d in WEEKDAYS:
            win = self.hours.get(d) or []
            label = d.capitalize()
            parts.append(f"{label}: " + (", ".join(f"{a}-{b}" for a, b in win) if win else "closed"))
        return "; ".join(parts)


def load_business(path: str) -> Business:
    with open(path, encoding="utf-8") as f:
        return Business.model_validate(yaml.safe_load(f))


@lru_cache
def get_business(path: str) -> Business:
    return load_business(path)
