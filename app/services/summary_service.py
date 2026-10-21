"""Structured call summaries built from server-side facts (not from model claims)."""
from __future__ import annotations

import logging
import re

from app.models.schemas import CallSummary, SessionState
from app.repositories.call_repository import CallRepository
from app.services.notification_service import NotificationService
from app.utils import utc_naive_to_ist

log = logging.getLogger(__name__)

INTENT_PRIORITY = ["cancel_appointment", "book_appointment", "availability_inquiry", "doctor_information",
                   "hospital_information", "human_assistance"]
OUTCOME_TEXT = {
    "appointment_confirmed": "An appointment was booked and confirmed by the hospital system.",
    "appointment_pending": "An appointment request was submitted and is pending.",
    "booking_failed": "Booking could not be completed.",
    "booking_uncertain": "Booking outcome is uncertain; staff verification required.",
    "appointment_cancelled": "An appointment was cancelled (confirmed by the hospital system).",
    "cancellation_escalated": "Cancellation needs staff verification.",
    "handed_off_to_staff": "Caller was handed off to staff / callback requested.",
    "information_provided": "Information was provided.",
    "no_action": "No action was taken.",
}
_DIGITS = re.compile(r"\+?\d[\d\s\-]{6,}\d")


def pick_intent(intents: list[str]) -> str:
    for i in INTENT_PRIORITY:
        if i in intents:
            return i
    return intents[0] if intents else "unknown"


class SummaryService:
    def __init__(self, calls: CallRepository, notifications: NotificationService, retain_transcripts: bool):
        self.calls, self.notifications, self.retain = calls, notifications, retain_transcripts

    def build(self, call_id: str, final: bool = False) -> CallSummary:
        row = self.calls.get(call_id)
        if row is None:
            raise KeyError(call_id)
        state = SessionState.model_validate_json(row.state_json or "{}")
        f = state.facts
        intent = pick_intent(f.intents)
        text = f"Caller intent: {intent.replace('_', ' ')}. {OUTCOME_TEXT.get(f.outcome, f.outcome)}"
        if f.appointment_ref:
            text += f" Appointment reference: {f.appointment_ref}."
        if f.callback_required:
            text += " A staff callback is required."
        if f.notes:
            text += " Note: " + _DIGITS.sub("[number]", f.notes)[:200]
        started = utc_naive_to_ist(row.started_at).isoformat(timespec="seconds")
        return CallSummary(call_id=call_id, call_timestamp=started, caller_intent=intent, outcome=f.outcome,
                           appointment_reference=f.appointment_ref, callback_required=f.callback_required,
                           summary=text, final=final)

    async def finalize(self, call_id: str) -> CallSummary:
        """Idempotent: complete the call, store the summary, purge transcript, send summary once."""
        row = self.calls.get(call_id)
        if row is None:
            raise KeyError(call_id)
        if row.status == "completed" and row.summary_json:
            return CallSummary.model_validate_json(row.summary_json)
        summary = self.build(call_id, final=True)
        state = self.calls.load_state(call_id)
        self.calls.complete(call_id, summary.model_dump(), purge_transcript=not self.retain, state=state)
        try:
            await self.notifications.send_call_summary(summary.model_dump())
            self.calls.mark_summary_sent(call_id)
        except Exception:  # noqa: BLE001
            log.exception("sending call summary failed")
        return summary
