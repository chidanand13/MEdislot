from __future__ import annotations

import abc
import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.utils import mask_email

log = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None


class EmailProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def send(self, to: str, subject: str, body: str) -> ProviderResult: ...


@dataclass
class MockEmailProvider(EmailProvider):
    """Logs a SAFE line (no body) and keeps messages in memory for tests. Sends nothing."""
    name: str = "mock"
    sent: list[dict] = field(default_factory=list)

    async def send(self, to: str, subject: str, body: str) -> ProviderResult:
        self.sent.append({"to": to, "subject": subject, "body": body})
        log.info("[MOCK EMAIL] to=%s subject=%s", mask_email(to), subject)
        return ProviderResult(ok=True, message_id=f"mock-email-{len(self.sent)}")


class SMTPEmailProvider(EmailProvider):
    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str, sender: str):
        self.host, self.port, self.username, self.password, self.sender = host, port, username, password, sender

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> ProviderResult:
        try:
            await asyncio.to_thread(self._send_sync, to, subject, body)
            return ProviderResult(ok=True, message_id="smtp")
        except Exception as exc:  # noqa: BLE001 - report any delivery failure
            return ProviderResult(ok=False, error=f"{type(exc).__name__}")
