"""Twilio adapter: <Gather input="speech"> (Twilio STT) + <Say> (Twilio TTS) over TwiML webhooks.
Signature verification follows Twilio's documented HMAC-SHA1 scheme (X-Twilio-Signature)."""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from fastapi import Request, Response

from app.config import Settings
from app.providers.telephony.base import CallEvent, InboundCall, TelephonyProvider, VoiceReply, COMPLETED_STATUSES


def compute_twilio_signature(auth_token: str, url: str, params: dict[str, Any]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TwilioTelephonyProvider(TelephonyProvider):
    name = "twilio"
    is_live = True

    def __init__(self, settings: Settings):
        self.token = settings.twilio_auth_token.get_secret_value()
        self.public_base = settings.public_base_url.rstrip("/")
        self.language = settings.stt_language
        self.voice = settings.tts_voice

    async def read_params(self, request: Request) -> dict[str, Any]:
        form = await request.form()
        return {k: str(v) for k, v in form.items()}

    def verify(self, request: Request, params: dict[str, Any]) -> bool:
        sig = request.headers.get("X-Twilio-Signature", "")
        if not sig:
            return False
        url = self.public_base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        expected = compute_twilio_signature(self.token, url, params)
        return hmac.compare_digest(sig, expected)

    def parse_inbound(self, params: dict[str, Any]) -> InboundCall:
        return InboundCall(call_sid=params.get("CallSid", ""), from_number=params.get("From"))

    def parse_event(self, params: dict[str, Any]) -> CallEvent:
        sid = params.get("CallSid", "")
        status = params.get("CallStatus", "")
        if status in COMPLETED_STATUSES:
            return CallEvent(sid, "status", status=status)
        text = (params.get("SpeechResult") or "").strip()
        return CallEvent(sid, "speech" if text else "silence", speech_text=text)

    def render(self, reply: VoiceReply, action_url: str) -> Response:
        say = f'<Say voice={quoteattr(self.voice)} language={quoteattr(self.language)}>{escape(reply.say)}</Say>'
        if reply.hangup:
            body = say + (f"<Dial>{escape(reply.transfer_to)}</Dial>" if reply.transfer_to else "<Hangup/>")
        else:
            act = quoteattr(action_url)
            body = (f'<Gather input="speech" action={act} method="POST" language={quoteattr(self.language)} '
                    f'speechTimeout="auto" timeout="6">{say}</Gather><Redirect method="POST">{escape(action_url)}</Redirect>')
        return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                        media_type="application/xml")
