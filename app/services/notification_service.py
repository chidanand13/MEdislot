"""Notifications with interchangeable providers, delivery tracking and duplicate prevention."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings
from app.providers.email.base import EmailProvider, ProviderResult
from app.providers.sms.base import SMSProvider
from app.repositories.notification_repository import NotificationRepository
from app.utils import mask_email, mask_phone

log = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    sent: bool
    duplicate: bool = False
    error: str | None = None


class NotificationService:
    def __init__(self, settings: Settings, repo: NotificationRepository, email: EmailProvider, sms: SMSProvider):
        self.s, self.repo, self.email, self.sms = settings, repo, email, sms

    async def _deliver(self, dedupe_key: str, kind: str, channel: str, recipient: str, send_coro_factory) -> DeliveryResult:
        masked = mask_email(recipient) if channel == "email" else mask_phone(recipient)
        if not self.repo.claim(dedupe_key, kind, channel, masked):
            log.info("notification skipped (duplicate) key=%s", dedupe_key)
            return DeliveryResult(sent=False, duplicate=True)
        try:
            res: ProviderResult = await send_coro_factory()
        except Exception as exc:  # noqa: BLE001
            res = ProviderResult(ok=False, error=type(exc).__name__)
        self.repo.finish(dedupe_key, res.ok, res.message_id, res.error)
        if not res.ok:
            log.warning("notification failed kind=%s channel=%s error=%s", kind, channel, res.error)
        return DeliveryResult(sent=res.ok, error=res.error)

    # ---- patient notifications (SMS) ----
    async def send_booking_confirmation(self, *, appointment_id: str, patient_name: str, patient_phone: str,
                                        doctor_name: str, date: str, start_time: str) -> DeliveryResult:
        body = (f"{self.s.hospital_name}: Hello {patient_name}, your appointment with {doctor_name} is confirmed "
                f"for {date} at {start_time} IST. Booking reference: {appointment_id}.")
        return await self._deliver(f"confirm:{appointment_id}", "booking_confirmation", "sms", patient_phone,
                                   lambda: self.sms.send(patient_phone, body))

    async def send_reminder(self, *, reminder_id: int, appointment_id: str, patient_name: str, patient_phone: str,
                            doctor_name: str, date: str, start_time: str) -> DeliveryResult:
        body = (f"{self.s.hospital_name} reminder: Hello {patient_name}, you have an appointment with {doctor_name} "
                f"on {date} at {start_time} IST. Booking reference: {appointment_id}.")
        return await self._deliver(f"reminder:{reminder_id}", "appointment_reminder", "sms", patient_phone,
                                   lambda: self.sms.send(patient_phone, body))

    # ---- staff notifications ----
    async def alert_staff(self, kind: str, subject: str, body: str, dedupe_key: str) -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        subject = f"[MediSlot alert] {subject}"
        if self.s.staff_alert_email or self.email.name == "mock":
            to = self.s.staff_alert_email or "staff@example.invalid"
            results.append(await self._deliver(f"{dedupe_key}:email", f"staff_{kind}", "email", to,
                                               lambda: self.email.send(to, subject, body)))
        if self.s.staff_alert_phone:
            phone = self.s.staff_alert_phone
            results.append(await self._deliver(f"{dedupe_key}:sms", f"staff_{kind}", "sms", phone,
                                               lambda: self.sms.send(phone, f"{subject}: {body}"[:300])))
        return results

    async def send_call_summary(self, summary: dict) -> list[DeliveryResult]:
        text = (f"Call ID: {summary['call_id']}\nTime: {summary['call_timestamp']}\nIntent: {summary['caller_intent']}\n"
                f"Outcome: {summary['outcome']}\nAppointment ref: {summary.get('appointment_reference') or '-'}\n"
                f"Callback required: {'YES' if summary['callback_required'] else 'no'}\n\n{summary['summary']}")
        to = self.s.summary_email or "staff@example.invalid"
        results = [await self._deliver(f"summary:{summary['call_id']}:email", "call_summary", "email", to,
                                       lambda: self.email.send(to, f"Call summary {summary['call_id'][:8]}", text))]
        if self.s.staff_alert_phone and summary["callback_required"]:
            phone = self.s.staff_alert_phone
            short = f"Call {summary['call_id'][:8]}: {summary['outcome']}. Callback required."
            results.append(await self._deliver(f"summary:{summary['call_id']}:sms", "call_summary", "sms", phone,
                                               lambda: self.sms.send(phone, short)))
        return results
