"""Mock telephony: JSON in / JSON out. Lets you 'phone' the app from Postman. No real calls."""
from __future__ import annotations

from typing import Any

from fastapi import Request, Response

from app.providers.telephony.base import CallEvent, InboundCall, TelephonyProvider, VoiceReply, COMPLETED_STATUSES
import json


class MockTelephonyProvider(TelephonyProvider):
    name = "mock"
    is_live = False

    async def read_params(self, request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def verify(self, request: Request, params: dict[str, Any]) -> bool:
        return True  # mock mode only; the internal API key still protects nothing here by design

    def parse_inbound(self, params: dict[str, Any]) -> InboundCall:
        return InboundCall(call_sid=str(params.get("call_sid") or "MOCK-CALL"), from_number=params.get("from"))

    def parse_event(self, params: dict[str, Any]) -> CallEvent:
        sid = str(params.get("call_sid") or "MOCK-CALL")
        status = str(params.get("status") or "")
        if status in COMPLETED_STATUSES:
            return CallEvent(sid, "status", status=status)
        text = str(params.get("speech") or "").strip()
        return CallEvent(sid, "speech" if text else "silence", speech_text=text)

    def render(self, reply: VoiceReply, action_url: str) -> Response:
        body = {"mode": "mock", "say": reply.say, "listen": reply.listen, "hangup": reply.hangup,
                "transfer_to": reply.transfer_to, "next_url": action_url}
        return Response(content=json.dumps(body), media_type="application/json")
