"""OpenAI tool schemas + the TRUSTED Python executor.

The model only *requests* a tool; this module validates the arguments with Pydantic and runs plain Python.
Nothing the model says is treated as proof that an operation succeeded: results come from the HMS client.

Server-side confirmation gate (booking and cancellation):
  1. prepare_booking validates a live slot and stores the exact details in the session.
  2. book_appointment executes only if (a) details match what was prepared, (b) at least one NEW caller
     message arrived after preparation, and (c) that latest caller message is an explicit affirmative.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.models.schemas import (BookingArgs, CancelArgs, CheckAvailabilityArgs, FaqArgs, HandoffArgs,
                                ListDoctorsArgs, SessionState, SummaryArgs)
from app.services.appointment_service import AppointmentService
from app.services.faq_service import FaqService
from app.services.handoff_service import HandoffService
from app.utils import last4

log = logging.getLogger(__name__)

_YES = re.compile(r"\b(yes|yeah|yep|yup|confirm(ed)?|correct|that'?s right|go ahead|please do|sure|ok(ay)?|"
                  r"haan|sounds good|proceed|book it|do it)\b", re.I)
_NO = re.compile(r"\b(no|not|don'?t|do not|wait|stop|change|wrong|incorrect|cancel that|never ?mind)\b", re.I)


def is_affirmative(text: str) -> bool:
    return bool(_YES.search(text or "")) and not _NO.search(text or "")


def _fn(name: str, description: str, model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for p in schema.get("properties", {}).values():
        p.pop("title", None)
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


TOOL_DEFINITIONS: list[dict] = [
    _fn("lookup_faq", "Look up an APPROVED hospital FAQ answer (hours, location, departments, booking process, "
        "contacting staff). Use for any hospital-information question.", FaqArgs),
    _fn("list_doctors", "List active doctors and specialties from the hospital system. Optionally filter by specialty.",
        ListDoctorsArgs),
    _fn("check_availability", "Check live appointment slots for a doctor_id OR a specialty on a date (YYYY-MM-DD, IST).",
        CheckAvailabilityArgs),
    _fn("prepare_booking", "Validate that the chosen slot is really available and stage the booking for confirmation. "
        "Call this once you have doctor_id, date, start_time (HH:mm), patient_name and patient_phone. "
        "Then read ALL details back and ask for an explicit yes. This does NOT book.", BookingArgs),
    _fn("book_appointment", "Submit the booking. ONLY call after prepare_booking AND the caller explicitly said yes in "
        "their latest message. The server rejects it otherwise.", BookingArgs),
    _fn("cancel_appointment", "Cancel an appointment. Needs appointment_id plus the patient's name and phone as "
        "verification. Set caller_confirms_cancellation true only after the caller explicitly agreed.", CancelArgs),
    _fn("request_human_handoff", "Request a staff callback / human handoff (caller asks for a human, unsupported request, "
        "repeated misunderstanding, system problems, or uncertain booking).", HandoffArgs),
    _fn("generate_call_summary", "Record the caller's intent/outcome notes. Set call_finished true when the caller has "
        "no more requests and the call should end.", SummaryArgs),
]

_INTENT_BY_TOOL = {"lookup_faq": "hospital_information", "list_doctors": "doctor_information",
                   "check_availability": "availability_inquiry", "prepare_booking": "book_appointment",
                   "book_appointment": "book_appointment", "cancel_appointment": "cancel_appointment",
                   "request_human_handoff": "human_assistance"}
_RANK = {"no_action": 0, "information_provided": 1, "handed_off_to_staff": 2, "booking_failed": 3,
         "booking_uncertain": 3, "cancellation_escalated": 3, "appointment_pending": 4,
         "appointment_confirmed": 4, "appointment_cancelled": 4}


def set_outcome(state: SessionState, outcome: str) -> None:
    if _RANK.get(outcome, 0) >= _RANK.get(state.facts.outcome, 0):
        state.facts.outcome = outcome


@dataclass
class ToolContext:
    call_id: str | None
    caller_number: str | None
    state: SessionState


class ToolExecutor:
    def __init__(self, appointments: AppointmentService, faq: FaqService, handoff: HandoffService):
        self.appointments, self.faq, self.handoff = appointments, faq, handoff

    async def execute(self, name: str, raw_arguments: str, ctx: ToolContext) -> dict[str, Any]:
        """Never raises: every failure becomes a structured result the model can read."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"error": "unknown_tool"}
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError:
            ctx.state.tool_failures += 1
            return {"error": "malformed_arguments", "message": "Arguments were not valid JSON. Ask the caller again."}
        if name in _INTENT_BY_TOOL and _INTENT_BY_TOOL[name] not in ctx.state.facts.intents:
            ctx.state.facts.intents.append(_INTENT_BY_TOOL[name])
        try:
            return await handler(args, ctx)
        except ValidationError as exc:
            ctx.state.tool_failures += 1
            problems = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()][:4]
            return {"error": "invalid_arguments", "problems": problems,
                    "message": "Ask the caller for the missing/invalid details. Do not guess."}
        except Exception:  # noqa: BLE001
            log.exception("tool %s crashed", name)
            ctx.state.tool_failures += 1
            return {"error": "internal_error", "message": "That tool failed. Offer a staff callback."}

    # ---- read-only tools ----
    async def _t_lookup_faq(self, args, ctx):
        return self.faq.lookup(FaqArgs(**args).question)

    async def _t_list_doctors(self, args, ctx):
        a = ListDoctorsArgs(**args)
        res = await self.appointments.list_doctors(a.specialty)
        if res["status"] == "ok":
            set_outcome(ctx.state, "information_provided")
        return res

    async def _t_check_availability(self, args, ctx):
        a = CheckAvailabilityArgs(**args)
        res = await self.appointments.check_availability(a.date, a.doctor_id, a.specialty)
        if res["status"] in ("ok", "no_slots", "doctor_on_leave"):
            set_outcome(ctx.state, "information_provided")
        if res["status"] == "api_unavailable":
            res["next_step"] = "Apologise and call request_human_handoff."
        return res

    # ---- booking ----
    async def _t_prepare_booking(self, args, ctx):
        a = BookingArgs(**args)
        check = await self.appointments.verify_slot(a.doctor_id, a.date, a.start_time)
        if check["status"] != "available":
            ctx.state.pending_booking = None
            return check
        ctx.state.pending_booking = {**a.model_dump(), "doctor_name": check["doctor_name"],
                                     "prepared_turn": ctx.state.turn_index}
        return {"status": "ready_for_confirmation",
                "read_back": {"doctor": check["doctor_name"], "date": a.date, "time": a.start_time,
                              "patient_name": a.patient_name, "phone_ending": last4(a.patient_phone)},
                "instruction": "Read back doctor, date, time, name and phone, then ask the caller to say yes to confirm. "
                               "Do NOT call book_appointment until they say yes in their NEXT message."}

    async def _t_book_appointment(self, args, ctx):
        a = BookingArgs(**args)
        p = ctx.state.pending_booking
        same = p and all(p[k] == v for k, v in a.model_dump().items())
        if not same:
            return {"status": "not_prepared", "message": "Call prepare_booking with these exact details first."}
        if ctx.state.turn_index <= p["prepared_turn"] or not is_affirmative(ctx.state.last_user_text):
            return {"status": "confirmation_required",
                    "message": "The caller has not explicitly confirmed. Read the details back and ask for a yes."}
        res = await self.appointments.book(doctor_id=a.doctor_id, date=a.date, start_time=a.start_time,
                                           patient_name=a.patient_name, patient_phone=a.patient_phone,
                                           call_id=ctx.call_id)
        ctx.state.pending_booking = None
        st = res["status"]
        if st in ("confirmed", "duplicate"):
            set_outcome(ctx.state, "appointment_confirmed")
            ctx.state.facts.appointment_ref = res.get("appointment_id")
        elif st == "pending":
            set_outcome(ctx.state, "appointment_pending")
            ctx.state.facts.appointment_ref = res.get("appointment_id")
        elif st == "uncertain":
            set_outcome(ctx.state, "booking_uncertain")
        elif st == "failed":
            set_outcome(ctx.state, "booking_failed")
        if res.get("handoff_needed"):
            res["handoff"] = await self._handoff(ctx, f"Booking {st}; staff verification needed",
                                                 patient_name=a.patient_name)
        res.pop("handoff_needed", None)
        return res

    # ---- cancellation ----
    async def _t_cancel_appointment(self, args, ctx):
        a = CancelArgs(**args)
        v = a.verification_context
        booking = self.appointments.verify_cancellation(a.appointment_id, v.patient_name, v.patient_phone)
        if booking is None:
            set_outcome(ctx.state, "cancellation_escalated")
            h = await self._handoff(ctx, "Cancellation requires staff verification", patient_name=v.patient_name)
            return {"status": "needs_staff_verification", "handoff": h,
                    "message": "The caller's identity could not be verified automatically. Nothing was cancelled. "
                               "Tell the caller that staff will verify and call them back."}
        pc = ctx.state.pending_cancel
        if not (pc and pc["appointment_id"] == a.appointment_id):
            ctx.state.pending_cancel = {"appointment_id": a.appointment_id, "prepared_turn": ctx.state.turn_index}
            return {"status": "confirmation_required", "appointment": booking,
                    "message": "Verified. Read back doctor/date/time and ask the caller to say yes to cancel."}
        if (ctx.state.turn_index <= pc["prepared_turn"] or not v.caller_confirms_cancellation
                or not is_affirmative(ctx.state.last_user_text)):
            return {"status": "confirmation_required", "appointment": booking,
                    "message": "Ask for an explicit yes before cancelling."}
        res = await self.appointments.cancel(a.appointment_id)
        ctx.state.pending_cancel = None
        if res["status"] == "cancelled":
            set_outcome(ctx.state, "appointment_cancelled")
            ctx.state.facts.appointment_ref = a.appointment_id
        elif res.get("handoff_needed"):
            res["handoff"] = await self._handoff(ctx, f"Cancellation {res['status']}", patient_name=v.patient_name)
        res.pop("handoff_needed", None)
        return res

    # ---- handoff & summary ----
    async def _handoff(self, ctx: ToolContext, reason: str, patient_name: str | None = None) -> dict:
        ctx.state.facts.callback_required = True
        if "human_assistance" not in ctx.state.facts.intents:
            ctx.state.facts.intents.append("human_assistance")
        set_outcome(ctx.state, "handed_off_to_staff")
        if ctx.state.handoff_done:
            return {"callback_requested": True, "duplicate": True}
        res = await self.handoff.request(reason, call_id=ctx.call_id, caller_number=ctx.caller_number,
                                         patient_name=patient_name)
        ctx.state.handoff_done = True
        return {"callback_requested": True, **res}

    async def _t_request_human_handoff(self, args, ctx):
        a = HandoffArgs(**args)
        res = await self._handoff(ctx, a.reason)
        return {**res, "message": "Tell the caller a staff member will call them back."}

    async def _t_generate_call_summary(self, args, ctx):
        a = SummaryArgs(**args)
        if a.notes:
            ctx.state.facts.notes = a.notes
        if a.call_finished:
            ctx.state.closed = True
        return {"status": "recorded", "call_finished": a.call_finished}
