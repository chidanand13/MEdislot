from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field

import httpx

from app.providers.email.base import ProviderResult
from app.utils import mask_phone

log = logging.getLogger(__name__)


class SMSProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def send(self, to: str, body: str) -> ProviderResult: ...


@dataclass
class MockSMSProvider(SMSProvider):
    name: str = "mock"
    sent: list[dict] = field(default_factory=list)

    async def send(self, to: str, body: str) -> ProviderResult:
        self.sent.append({"to": to, "body": body})
        log.info("[MOCK SMS] to=%s chars=%d", mask_phone(to), len(body))
        return ProviderResult(ok=True, message_id=f"mock-sms-{len(self.sent)}")


class TwilioSMSProvider(SMSProvider):
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.sid, self.token, self.from_number = account_sid, auth_token, from_number
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)

    async def send(self, to: str, body: str) -> ProviderResult:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            r = await self._client.post(url, data={"To": to, "From": self.from_number, "Body": body},
                                        auth=(self.sid, self.token))
        except httpx.HTTPError as exc:
            return ProviderResult(ok=False, error=type(exc).__name__)
        if r.status_code >= 400:
            return ProviderResult(ok=False, error=f"HTTP {r.status_code}")
        return ProviderResult(ok=True, message_id=r.json().get("sid"))
