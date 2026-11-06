"""VaaniDesk — real-time multilingual voice AI receptionist.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.accounts import router as accounts_router
from .api.calendar import router as calendar_router
from .api.dashboard import router as dashboard_router
from .api.knowledge import router as knowledge_router
from .api.opd import router as opd_router
from .api.report import router as report_router
from .background import drain
from .config import ROOT_DIR, Settings, get_settings
from .db import DEFAULT_CLINIC, current_clinic
from .security import require_admin, require_owner, require_superadmin
from .state import AppState
from .transports.browser import router as browser_router
from .transports.exotel import router as exotel_router
from .transports.twilio import router as twilio_router

STATIC_DIR = ROOT_DIR / "static"
NO_CACHE = {"Cache-Control": "no-cache"}  # always serve the latest page after an update


async def _purge_transcripts(container) -> None:
    """Delete call transcripts older than each clinic's business.transcript_retention_days."""
    log = logging.getLogger("vaanidesk")
    for clinic_id, tenant in list(container.tenants.items()):
        token = current_clinic.set(clinic_id)
        try:
            days = tenant.business.transcript_retention_days
            if n := await container.db.purge_transcripts(days):
                log.info("[%s] deleted transcripts of %d calls older than %d days", clinic_id, n, days)
        except Exception:
            log.exception("Transcript retention cleanup failed for %s", clinic_id)
        finally:
            current_clinic.reset(token)


async def _reminder_loop(container, every_s: int = 600) -> None:
    """Every 10 minutes: send tomorrow's appointment / token reminders for clinics whose send time has passed."""
    from .tools.reminders import send_reminders
    log = logging.getLogger("vaanidesk")
    while True:
        await asyncio.sleep(every_s)
        for clinic_id, tenant in list(container.tenants.items()):
            token = current_clinic.set(clinic_id)
            try:
                await asyncio.shield(send_reminders(container.db, tenant.business, container.twilio))
            except Exception:
                log.exception("Reminders failed for %s", clinic_id)
            finally:
                current_clinic.reset(token)


async def _retention_loop(container) -> None:
    """Daily cleanup (the first run happens at startup, before serving)."""
    while True:
        await asyncio.sleep(24 * 3600)
        await asyncio.shield(_purge_transcripts(container))


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = state or AppState.build(settings)
        await container.db.create_all()
        await container.load_clinics()
        try:
            changed = await container.knowledge.sync_folder(Path(container.settings.knowledge_dir))
            if changed:
                logging.getLogger("vaanidesk").info("Knowledge base updated: %s", changed)
        except Exception:  # never block startup on the knowledge base
            logging.getLogger("vaanidesk").exception("Knowledge base sync failed")
        app.state.container = container
        await _purge_transcripts(container)
        retention = asyncio.create_task(_retention_loop(container))
        reminders = asyncio.create_task(_reminder_loop(container))
        if state is None and (missing := settings.missing_keys()):
            logging.getLogger("vaanidesk").warning("Live calls disabled until these are set in .env: %s",
                                                   ", ".join(missing))
        yield
        retention.cancel()
        reminders.cancel()
        await asyncio.gather(retention, reminders, return_exceptions=True)
        for session in list(container.active_sessions.values()):
            await session.close()
        await drain()  # let SMS / summaries finish before the database closes
        await container.db.dispose()

    app = FastAPI(title="VaaniDesk", version="1.0.0", lifespan=lifespan,
                  description="Real-time multilingual voice AI receptionist (English, Hindi, Telugu)")
    app.include_router(twilio_router)
    app.include_router(browser_router)
    app.include_router(exotel_router)
    app.include_router(dashboard_router)
    app.include_router(calendar_router)
    app.include_router(knowledge_router)
    app.include_router(opd_router)
    app.include_router(accounts_router)
    app.include_router(report_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/demo")

    @app.get("/demo", include_in_schema=False)
    async def demo():
        return FileResponse(STATIC_DIR / "demo.html", headers=NO_CACHE)

    @app.get("/dashboard", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def dashboard():
        return FileResponse(STATIC_DIR / "dashboard.html", headers=NO_CACHE)

    @app.get("/calendar", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def calendar_page():
        return FileResponse(STATIC_DIR / "calendar.html", headers=NO_CACHE)

    @app.get("/opd", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def opd_page():
        return FileResponse(STATIC_DIR / "opd.html", headers=NO_CACHE)

    @app.get("/report", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def report_page():
        return FileResponse(STATIC_DIR / "report.html", headers=NO_CACHE)

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(STATIC_DIR / "login.html", headers=NO_CACHE)

    @app.get("/signup", include_in_schema=False)
    async def signup_page():
        return FileResponse(STATIC_DIR / "signup.html", headers=NO_CACHE)

    @app.get("/setup", include_in_schema=False, dependencies=[Depends(require_owner)])
    async def setup_page():
        return FileResponse(STATIC_DIR / "setup.html", headers=NO_CACHE)

    @app.get("/admin", include_in_schema=False, dependencies=[Depends(require_superadmin)])
    async def admin_page():
        return FileResponse(STATIC_DIR / "admin.html", headers=NO_CACHE)

    @app.get("/knowledge", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def knowledge_page():
        return FileResponse(STATIC_DIR / "knowledge.html", headers=NO_CACHE)

    @app.get("/health")
    async def health():
        c = app.state.container
        return {"status": "ok", "business": c.business.name, "clinics": len(c.tenants),
                "active_calls": len(c.active_sessions), "missing_keys": c.settings.missing_keys(),
                "stt": c.settings.stt_provider, "llm": c.settings.openai_model, "twilio": c.settings.twilio_enabled}

    @app.get("/api/public-config")
    async def public_config(request: Request, clinic: str | None = None):
        """Public info for the demo page; ?clinic=<id> picks the clinic (default: the first one)."""
        c = app.state.container
        if clinic and clinic not in c.tenants:
            raise HTTPException(404, "clinic not found")
        biz = c.tenant(clinic or DEFAULT_CLINIC).business
        return {"business": biz.name, "assistant": biz.assistant_name, "clinic": clinic or DEFAULT_CLINIC,
                "languages": biz.languages, "access_code_required": bool(c.settings.demo_access_code),
                "emergency_number": biz.emergency_number,
                "phone_number": c.settings.twilio_phone_number or None}

    return app


app = create_app()
