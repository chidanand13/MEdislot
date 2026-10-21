"""Shared fixtures. All external services are mocked; no real appointments or messages are ever created."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.providers.email.base import MockEmailProvider
from app.providers.sms.base import MockSMSProvider
from app.services.openai_service import AssistantMessage, LLMError, ToolCall
from app.utils import today_ist

HMS = "https://akhil12.app.n8n.cloud/webhook"
INTERNAL_KEY = "test-internal-key"
AUTH = {"X-Internal-Api-Key": INTERNAL_KEY}


def future_date(days: int = 5) -> str:
    return (today_ist() + timedelta(days=days)).isoformat()


def make_settings(tmp_path, **over) -> Settings:
    base = dict(_env_file=None, database_url=f"sqlite:///{tmp_path/'t.db'}", hms_api_key="hms-secret",
                internal_api_key=INTERNAL_KEY, reminder_loop_enabled=False, hms_get_retries=1,
                staff_alert_email="staff@example.invalid", staff_alert_phone="+910000000001",
                faq_path="app/data/hospital_faqs.json", rate_limit_per_minute=1000)
    base.update(over)
    return Settings(**base)


class FakeLLM:
    """Scripted model: each script item is an AssistantMessage, an Exception, or a callable(messages)."""
    is_mock = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(messages) if callable(item) else item


def tc(name, args, id_="c1") -> AssistantMessage:
    return AssistantMessage(tool_calls=[ToolCall(id_, name, json.dumps(args) if not isinstance(args, str) else args)])


def say(text) -> AssistantMessage:
    return AssistantMessage(content=text)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def sms():
    return MockSMSProvider()


@pytest.fixture
def email():
    return MockEmailProvider()


@pytest.fixture
def mock_hms():
    with respx.mock(assert_all_called=False) as m:
        yield m


@pytest.fixture
def make_container(settings, sms, email, mock_hms):
    containers = []

    def _make(llm=None, **kw):
        kw.setdefault("sms", sms)
        c = build_container(kw.pop("settings", settings), llm=llm or FakeLLM([]), email=email, **kw)
        containers.append(c)
        return c
    yield _make


def appt_payload(**over):
    d = {"id": "APT-261019-5YUQEO", "doctor_id": "DOC-001", "doctor_name": "Dr. Ananya Rao",
         "patient_name": "Asha Verma", "patient_phone": "+919876543210", "date": future_date(),
         "start_time": "10:00", "end_time": "10:15", "status": "confirmed"}
    d.update(over)
    return {"success": True, "data": d}


def slots_payload(date=None, doctor_id="DOC-001", name="Dr. Ananya Rao", slots=("10:00", "10:15")):
    return {"success": True, "data": {"doctor_id": doctor_id, "doctor_name": name, "date": date or future_date(),
                                      "slots": list(slots)}}


PATIENT = "+919876543210"


def patient_sms(sms):
    """Messages sent to the PATIENT (staff alerts go to a different number)."""
    return [m for m in sms.sent if m["to"] == PATIENT]
