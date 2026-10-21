"""Pydantic models: HMS data, tool arguments, session state and API payloads."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils import normalize_phone, valid_date, valid_time


# ---------------- HMS data ----------------
class Doctor(BaseModel):
    id: str
    name: str
    specialty: str | None = None


class DoctorList(BaseModel):
    doctors: list[Doctor] = []
    specialties: list[str] = []


class DoctorAvailability(BaseModel):
    doctor_id: str | None = None
    doctor_name: str | None = None
    specialty: str | None = None
    date: str | None = None
    slots: list[str] = []


# ---------------- session state ----------------
class Facts(BaseModel):
    intents: list[str] = []
    outcome: str = "no_action"
    appointment_ref: str | None = None
    callback_required: bool = False
    notes: str | None = None


class SessionState(BaseModel):
    messages: list[dict[str, Any]] = []
    turn_index: int = 0
    last_user_text: str = ""
    pending_booking: dict[str, Any] | None = None
    pending_cancel: dict[str, Any] | None = None
    misunderstood: int = 0
    tool_failures: int = 0
    handoff_done: bool = False
    closed: bool = False
    facts: Facts = Facts()


# ---------------- tool arguments ----------------
class ListDoctorsArgs(BaseModel):
    specialty: str | None = Field(default=None, max_length=60)


class CheckAvailabilityArgs(BaseModel):
    doctor_id: str | None = Field(default=None, max_length=40)
    specialty: str | None = Field(default=None, max_length=60)
    date: str

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not valid_date(v):
            raise ValueError("date must be a real date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def _one_target(self):
        if not self.doctor_id and not self.specialty:
            raise ValueError("provide doctor_id or specialty")
        return self


class BookingArgs(BaseModel):
    doctor_id: str = Field(min_length=1, max_length=40)
    date: str
    start_time: str
    patient_name: str = Field(min_length=2, max_length=80)
    patient_phone: str

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not valid_date(v):
            raise ValueError("date must be YYYY-MM-DD")
        return v

    @field_validator("start_time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not valid_time(v):
            raise ValueError("start_time must be HH:mm (24h)")
        return v

    @field_validator("patient_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not re.match(r"^[A-Za-z][A-Za-z .'\-]{1,79}$", v):
            raise ValueError("patient_name contains invalid characters")
        return v

    @field_validator("patient_phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_phone(v)


class VerificationContext(BaseModel):
    patient_name: str = Field(min_length=2, max_length=80)
    patient_phone: str
    caller_confirms_cancellation: bool = False

    @field_validator("patient_phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_phone(v)


class CancelArgs(BaseModel):
    appointment_id: str = Field(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9\-_]+$")
    verification_context: VerificationContext


class HandoffArgs(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


class SummaryArgs(BaseModel):
    intent: str = Field(default="", max_length=80)
    outcome: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=300)
    call_finished: bool = False


class FaqArgs(BaseModel):
    question: str = Field(min_length=2, max_length=300)


# ---------------- API payloads ----------------
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=1000)
    caller_number: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    ended: bool = False
    handoff_requested: bool = False


class CallbackCreate(BaseModel):
    reason: str = Field(min_length=3, max_length=300)
    caller_number: str | None = None
    patient_name: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=500)
    call_id: str | None = None


class TurnResult(BaseModel):
    reply: str
    end_call: bool = False
    handoff_requested: bool = False


class CallSummary(BaseModel):
    call_id: str
    call_timestamp: str
    caller_intent: str
    outcome: str
    appointment_reference: str | None = None
    callback_required: bool = False
    summary: str
    final: bool = True
