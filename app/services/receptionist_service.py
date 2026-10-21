"""Conversation engine: system prompt, deterministic safety rules, bounded tool-calling loop."""
from __future__ import annotations

import json
import logging
import re

from app.config import Settings
from app.models.schemas import SessionState, TurnResult
from app.repositories.call_repository import CallRepository
from app.services.openai_service import LLMClient, LLMError
from app.tools.appointment_tools import TOOL_DEFINITIONS, ToolContext, ToolExecutor, set_outcome
from app.utils import now_ist

log = logging.getLogger(__name__)

_EMERGENCY = re.compile(r"\b(chest pain|can'?t breathe|cannot breathe|difficulty breathing|not breathing|unconscious|"
                        r"heart attack|stroke|severe bleeding|bleeding heavily|overdose|suicid\w*|seizure|"
                        r"choking|emergency)\b", re.I)
_HUMAN = re.compile(r"\b(human|real person|live agent|representative|operator|(speak|talk) (to|with) (a |an )?"
                    r"(person|someone|staff|agent|nurse))\b", re.I)
_MAX_USER_TURNS_KEPT = 12


class ReceptionistService:
    def __init__(self, settings: Settings, llm: LLMClient, tools: ToolExecutor, calls: CallRepository):
        self.s, self.llm, self.tools, self.calls = settings, llm, tools, calls

    # ------------------------------------------------------------------ prompts
    def greeting(self) -> str:
        return (f"Hello, this is the virtual AI receptionist for {self.s.hospital_name}. I can answer general questions, "
                f"check doctor availability, and help book or cancel appointments. If this is a medical emergency, "
                f"please hang up and call {self.s.emergency_number} now. How can I help you today?")

    def system_prompt(self) -> str:
        today = now_ist().strftime("%A, %Y-%m-%d")
        return f"""You are the virtual AI receptionist for {self.s.hospital_name}. Today is {today} (Asia/Kolkata).
You are an AI, not a human; say so if asked. Keep replies short, natural and suitable for speech (no markdown, no lists of more than 4 items).

SCOPE: hospital FAQs, listing doctors/specialties, checking availability, booking, cancelling, and staff callbacks.
NEVER give diagnosis, prescriptions, treatment advice or emergency triage. For any emergency, tell the caller to contact
{self.s.emergency_number} or local emergency services immediately.

FACTS: Use ONLY tool results for doctors, schedules, slots, booking IDs and cancellation outcomes. Use lookup_faq for hospital
information and answer only from its result; if nothing is found, say you don't have that information and offer a staff callback.
Never invent doctors, times, IDs or hospital details. Say times in 12-hour or 24-hour form clearly, dates with the month name.

BOOKING: collect patient name, phone number, doctor or specialty, date, and a time chosen from the REAL slots returned by
check_availability. Then call prepare_booking, read back doctor, date, time, name and phone, and ask the caller to say yes.
Only after the caller explicitly says yes in a later message call book_appointment. Report exactly what the tool returns:
confirmed -> give the booking ID; pending -> say it is pending; conflict -> offer the returned alternatives; failed -> say the
booking could not be confirmed; uncertain -> say the outcome is unconfirmed, staff will check, and do NOT try booking again.

CANCELLATION: collect appointment ID, patient name and phone. Call cancel_appointment. If it asks for confirmation, read the
appointment back and get an explicit yes, then call again with caller_confirms_cancellation true. Say it is cancelled ONLY if the
tool returns status cancelled. If it needs staff verification, tell the caller staff will call back.

HANDOFF: call request_human_handoff if the caller wants a person, asks for something unsupported, you cannot understand them
after repeated attempts, or systems are unavailable. When the caller has no more requests, say goodbye and call
generate_call_summary with call_finished true. Ask only one question at a time."""

    # ------------------------------------------------------------------ turns
    async def handle_turn(self, call_id: str, user_text: str, caller_number: str | None = None) -> TurnResult:
        state = self.calls.load_state(call_id)
        user_text = (user_text or "").strip()[:1000]
        state.turn_index += 1
        state.last_user_text = user_text
        state.misunderstood = 0
        state.messages.append({"role": "user", "content": user_text})
        ctx = ToolContext(call_id=call_id, caller_number=caller_number, state=state)

        # Deterministic safety rules run BEFORE the model.
        if _EMERGENCY.search(user_text):
            reply = (f"This sounds like it may be an emergency. Please hang up and call {self.s.emergency_number} or your "
                     f"local emergency services right away, or go to the nearest emergency department. I cannot give "
                     f"medical advice. Is there anything else I can help with?")
            return self._finish(call_id, state, reply)
        if _HUMAN.search(user_text):
            await self.tools._handoff(ctx, "Caller asked for a human")
            return self._finish(call_id, state, "Of course. I have asked our staff to call you back. Is there anything else "
                                "I can help with in the meantime?", handoff=True)

        try:
            reply = await self._run_model(state, ctx)
        except LLMError:
            log.error("LLM unavailable during call=%s", call_id)
            await self.tools._handoff(ctx, "AI assistant unavailable")
            return self._finish(call_id, state, "I'm sorry, I'm having technical trouble. I have asked our staff to call "
                                "you back. Goodbye.", handoff=True, end=True)
        if state.tool_failures >= 3 and not state.handoff_done:
            await self.tools._handoff(ctx, "Repeated difficulty understanding the caller")
            reply += " I'm having trouble with that, so I've asked our staff to call you back."
            return self._finish(call_id, state, reply, handoff=True)
        return self._finish(call_id, state, reply, handoff=state.handoff_done)

    async def handle_silence(self, call_id: str, caller_number: str | None = None) -> TurnResult:
        state = self.calls.load_state(call_id)
        state.misunderstood += 1
        if state.misunderstood >= self.s.max_misunderstandings:
            ctx = ToolContext(call_id, caller_number, state)
            await self.tools._handoff(ctx, "Caller could not be understood")
            return self._finish(call_id, state, "I'm sorry, I'm having trouble hearing you. I've asked our staff to call "
                                "you back. Goodbye.", handoff=True, end=True)
        self.calls.save_state(call_id, state)
        return TurnResult(reply="Sorry, I didn't catch that. Could you please say it again?")

    # ------------------------------------------------------------------ internals
    async def _run_model(self, state: SessionState, ctx: ToolContext) -> str:
        used = 0
        limit = self.s.max_tool_calls_per_turn
        while True:
            messages = [{"role": "system", "content": self.system_prompt()}] + state.messages
            choice = "auto" if used < limit else "none"
            msg = await self.llm.chat(messages, TOOL_DEFINITIONS, tool_choice=choice)
            if msg.tool_calls and choice == "none":  # model ignored tool_choice=none; stop the loop
                msg.tool_calls = []
                msg.content = None
            if not msg.tool_calls:
                text = (msg.content or "").strip() or "Sorry, could you repeat that?"
                state.messages.append({"role": "assistant", "content": text})
                return text
            state.messages.append(msg.to_dict())
            for call in msg.tool_calls:
                if used >= limit:
                    result = {"error": "tool_call_limit_reached", "message": "Too many tool calls this turn."}
                else:
                    result = await self.tools.execute(call.name, call.arguments, ctx)
                    used += 1
                state.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})

    def _finish(self, call_id: str, state: SessionState, reply: str, handoff: bool = False,
                end: bool = False) -> TurnResult:
        if not state.messages or state.messages[-1].get("role") != "assistant" or state.messages[-1].get("content") != reply:
            state.messages.append({"role": "assistant", "content": reply})
        state.messages = self._trim(state.messages)
        end = end or state.closed
        self.calls.save_state(call_id, state)
        return TurnResult(reply=reply, end_call=end, handoff_requested=handoff)

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
        if len(idx) <= _MAX_USER_TURNS_KEPT:
            return messages
        return messages[idx[-_MAX_USER_TURNS_KEPT]:]
