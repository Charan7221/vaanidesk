"""VaaniDesk — real-time multilingual voice AI receptionist.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.calendar import router as calendar_router
from .api.dashboard import router as dashboard_router
from .api.knowledge import router as knowledge_router
from .config import ROOT_DIR, Settings, get_settings
from .security import require_admin
from .state import AppState
from .transports.browser import router as browser_router
from .transports.twilio import router as twilio_router

STATIC_DIR = ROOT_DIR / "static"
NO_CACHE = {"Cache-Control": "no-cache"}  # always serve the latest page after an update


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = state or AppState.build(settings)
        await container.db.create_all()
        try:
            changed = await container.knowledge.sync_folder(Path(container.settings.knowledge_dir))
            if changed:
                logging.getLogger("vaanidesk").info("Knowledge base updated: %s", changed)
        except Exception:  # never block startup on the knowledge base
            logging.getLogger("vaanidesk").exception("Knowledge base sync failed")
        app.state.container = container
        if state is None and (missing := settings.missing_keys()):
            logging.getLogger("vaanidesk").warning("Live calls disabled until these are set in .env: %s",
                                                   ", ".join(missing))
        yield
        for session in list(container.active_sessions.values()):
            await session.close()
        await container.db.dispose()

    app = FastAPI(title="VaaniDesk", version="1.0.0", lifespan=lifespan,
                  description="Real-time multilingual voice AI receptionist (English, Hindi, Telugu)")
    app.include_router(twilio_router)
    app.include_router(browser_router)
    app.include_router(dashboard_router)
    app.include_router(calendar_router)
    app.include_router(knowledge_router)
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

    @app.get("/knowledge", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def knowledge_page():
        return FileResponse(STATIC_DIR / "knowledge.html", headers=NO_CACHE)

    @app.get("/health")
    async def health():
        c = app.state.container
        return {"status": "ok", "business": c.business.name, "active_calls": len(c.active_sessions),
                "missing_keys": c.settings.missing_keys(), "stt": c.settings.stt_provider,
                "llm": c.settings.openai_model, "twilio": c.settings.twilio_enabled}

    @app.get("/api/public-config")
    async def public_config():
        c = app.state.container
        return {"business": c.business.name, "assistant": c.business.assistant_name,
                "languages": c.business.languages, "access_code_required": bool(c.settings.demo_access_code),
                "phone_number": c.settings.twilio_phone_number or None}

    return app


app = create_app()
