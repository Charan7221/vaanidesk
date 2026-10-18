from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.business import load_business
from app.config import ROOT_DIR, Settings
from app.db import Database
from app.tools.calendar import LocalCalendar
from app.tools.registry import ToolContext, ToolExecutor
from app.tools.twilio_client import TwilioClient


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        business_config_path=str(ROOT_DIR / "config" / "business.yaml"),
        openai_api_key="test", sarvam_api_key="test",
        admin_user="admin", admin_password="secret",
        twilio_validate_signature=False, end_of_turn_delay_ms=10,
    )


@pytest.fixture
def business(settings):
    return load_business(settings.business_config_path)


@pytest.fixture
async def db(settings):
    database = Database(settings.database_url)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
def calendar(db, business):
    return LocalCalendar(db, business)


@pytest.fixture
def executor(business, calendar, settings):
    return ToolExecutor(business, calendar, TwilioClient(settings))


@pytest.fixture
async def ctx(db):
    call = await db.create_call(channel="browser", caller="+919999999999")
    return ToolContext(call_id=call.id, channel="browser", caller_phone="+919999999999")


def next_weekday(business, weekday: int = 0) -> date:
    """Next date (at least tomorrow) falling on ``weekday`` (0=Mon)."""
    d = business.now().date() + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


@pytest.fixture
def monday(business) -> date:
    return next_weekday(business, 0)


@pytest.fixture
def at(business):
    def _at(day: date, hhmm: str) -> datetime:
        h, m = map(int, hhmm.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=business.tz)

    return _at
