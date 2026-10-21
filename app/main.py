"""MediSlot - AI Voice Receptionist + Notifications (development prototype)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import callbacks, chat, summaries, voice_webhooks
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.logging_config import configure_logging
from app.security import RateLimitMiddleware

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or build_container(settings)
        app.state.container = c
        if settings.reminders_enabled and settings.reminder_loop_enabled:
            c.reminders.start()
        log.info("MediSlot started env=%s hms_mock=%s llm_mock=%s telephony=%s email=%s sms=%s", settings.app_env,
                 settings.hms_is_mock, getattr(c.llm, "is_mock", False), c.telephony.name, c.email.name, c.sms.name)
        try:
            yield
        finally:  # graceful shutdown: stop reminder loop, close HTTP clients, dispose DB pool
            await c.aclose()
            log.info("MediSlot stopped")

    app = FastAPI(title="MediSlot AI Voice Receptionist", version="0.1.0-prototype", lifespan=lifespan)
    app.add_middleware(RateLimitMiddleware, limit_per_minute=settings.rate_limit_per_minute)
    for r in (chat.router, voice_webhooks.router, callbacks.router, summaries.router):
        app.include_router(r)

    @app.get("/health", tags=["system"])
    async def health():
        c = app.state.container
        return {"status": "ok", "prototype": True,
                "modes": {"hms": "mock" if c.settings.hms_is_mock else "live",
                          "llm": "mock" if getattr(c.llm, "is_mock", False) else "openai",
                          "telephony": c.telephony.name, "email": c.email.name, "sms": c.sms.name}}

    return app


app = create_app()  # used by: uvicorn app.main:app
