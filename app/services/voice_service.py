"""Call lifecycle orchestration (provider-agnostic)."""
from __future__ import annotations

import logging
import re

from app.config import Settings
from app.providers.telephony.base import CallEvent, InboundCall, VoiceReply
from app.repositories.call_repository import CallRepository
from app.services.receptionist_service import ReceptionistService
from app.services.summary_service import SummaryService

log = logging.getLogger(__name__)
_MD = re.compile(r"[*_`#>]+")


def to_speech(text: str) -> str:
    return re.sub(r"\s+", " ", _MD.sub("", text)).strip()[:900]


class VoiceService:
    def __init__(self, settings: Settings, calls: CallRepository, receptionist: ReceptionistService,
                 summaries: SummaryService):
        self.s, self.calls, self.receptionist, self.summaries = settings, calls, receptionist, summaries

    def start_call(self, inbound: InboundCall) -> tuple[str, VoiceReply]:
        row = self.calls.get_by_sid(inbound.call_sid) if inbound.call_sid else None
        if row is None:
            row = self.calls.create("voice", caller_number=inbound.from_number, provider_call_sid=inbound.call_sid or None)
        greeting = self.receptionist.greeting()
        state = self.calls.load_state(row.id)
        if not state.messages:
            state.messages.append({"role": "assistant", "content": greeting})
            self.calls.save_state(row.id, state)
        return row.id, VoiceReply(say=to_speech(greeting))

    async def handle_event(self, call_id: str, event: CallEvent) -> VoiceReply | None:
        """Returns a reply to speak, or None for status-only events."""
        row = self.calls.get(call_id)
        if row is None:
            raise KeyError(call_id)
        if event.kind == "status":
            await self.summaries.finalize(call_id)
            return None
        if row.status == "completed":
            return VoiceReply(say="This call has ended. Goodbye.", listen=False, hangup=True)
        if event.kind == "silence":
            turn = await self.receptionist.handle_silence(call_id, row.caller_number)
        else:
            turn = await self.receptionist.handle_turn(call_id, event.speech_text, row.caller_number)
        transfer = self.s.handoff_dial_number if (turn.handoff_requested and turn.end_call) else None
        reply = VoiceReply(say=to_speech(turn.reply), listen=not turn.end_call, hangup=turn.end_call,
                           transfer_to=transfer or None)
        if turn.end_call:
            await self.summaries.finalize(call_id)
        return reply

    async def error_fallback(self, call_id: str | None, caller_number: str | None = None) -> VoiceReply:
        """Used when something unexpected breaks mid-call: apologise, request a callback, hang up."""
        try:
            if call_id:
                state = self.calls.load_state(call_id)
                from app.tools.appointment_tools import ToolContext
                await self.receptionist.tools._handoff(ToolContext(call_id, caller_number, state), "Technical error during call")
                self.calls.save_state(call_id, state)
                await self.summaries.finalize(call_id)
        except Exception:  # noqa: BLE001
            log.exception("error fallback failed")
        return VoiceReply(say="I'm sorry, something went wrong on our side. Our staff will call you back. Goodbye.",
                          listen=False, hangup=True)
