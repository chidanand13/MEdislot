"""Dependency wiring. Tests build a Container with fakes; production builds it from Settings."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.providers.factory import build_email, build_sms, build_telephony
from app.repositories.booking_repository import BookingRepository
from app.repositories.call_repository import CallRepository
from app.repositories.callback_repository import CallbackRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.reminder_repository import ReminderRepository
from app.services.appointment_service import AppointmentService
from app.services.faq_service import FaqService
from app.services.handoff_service import HandoffService
from app.services.hms_api import HMSClient
from app.services.mock_hms import MOCK_BASE_URL, build_mock_transport
from app.services.notification_service import NotificationService
from app.services.openai_service import LLMClient, build_llm_client
from app.services.receptionist_service import ReceptionistService
from app.services.reminder_service import ReminderService
from app.services.summary_service import SummaryService
from app.services.voice_service import VoiceService
from app.tools.appointment_tools import ToolExecutor

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker
    hms: HMSClient
    llm: Any
    telephony: Any
    email: Any
    sms: Any
    calls: CallRepository
    callbacks: CallbackRepository
    notifications: NotificationService
    reminders: ReminderService
    appointments: AppointmentService
    handoff: HandoffService
    summaries: SummaryService
    receptionist: ReceptionistService
    voice: VoiceService

    async def aclose(self) -> None:
        await self.reminders.stop()
        await self.hms.aclose()
        if hasattr(self.llm, "aclose"):
            await self.llm.aclose()
        self.engine.dispose()


def build_container(settings: Settings, *, hms_transport: httpx.AsyncBaseTransport | None = None,
                    llm: LLMClient | None = None, email=None, sms=None, telephony=None) -> Container:
    if settings.app_env == "production" and settings.hms_is_mock:
        raise RuntimeError("Refusing to start in production without HMS_API_KEY (mock HMS is development-only)")
    engine = make_engine(settings.database_url)
    init_db(engine)
    sf = make_session_factory(engine)

    if hms_transport is not None:
        hms = HMSClient(settings.hms_base_url, settings.hms_api_key.get_secret_value() or "test-key",
                        settings.hms_timeout_seconds, settings.hms_get_retries, transport=hms_transport, backoff=0.0,
                        path_prefix=settings.hms_path_prefix)
    elif settings.hms_is_mock:
        log.warning("HMS MOCK MODE: using a local FICTIONAL HMS. No real appointments are created.")
        hms = HMSClient(MOCK_BASE_URL, "mock-key", settings.hms_timeout_seconds, 0, transport=build_mock_transport(), path_prefix="/hms")
    else:
        hms = HMSClient(settings.hms_base_url, settings.hms_api_key.get_secret_value(),
                        settings.hms_timeout_seconds, settings.hms_get_retries,
                        path_prefix=settings.hms_path_prefix)

    email = email or build_email(settings)
    sms = sms or build_sms(settings)
    telephony = telephony or build_telephony(settings)
    llm = llm or build_llm_client(settings)
    if settings.app_env == "production" and not telephony.is_live:
        raise RuntimeError("Refusing to start in production with mock telephony (webhooks would be unauthenticated)")

    calls = CallRepository(sf)
    callbacks = CallbackRepository(sf)
    notifications = NotificationService(settings, NotificationRepository(sf), email, sms)
    reminders = ReminderService(settings, ReminderRepository(sf), notifications)
    appointments = AppointmentService(settings, hms, BookingRepository(sf), notifications, reminders)
    handoff = HandoffService(callbacks, notifications)
    summaries = SummaryService(calls, notifications, settings.retain_transcripts)
    tools = ToolExecutor(appointments, FaqService(settings.faq_path), handoff)
    receptionist = ReceptionistService(settings, llm, tools, calls)
    voice = VoiceService(settings, calls, receptionist, summaries)
    return Container(settings, engine, sf, hms, llm, telephony, email, sms, calls, callbacks, notifications,
                     reminders, appointments, handoff, summaries, receptionist, voice)
