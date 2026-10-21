"""Persistent appointment reminders (survive restarts; no duplicates; cancelled => not sent)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from app.config import Settings
from app.repositories.reminder_repository import ReminderRepository
from app.services.notification_service import NotificationService
from app.utils import appointment_start_ist, now_ist, to_utc_naive, utcnow

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=5)


class ReminderService:
    def __init__(self, settings: Settings, repo: ReminderRepository, notifications: NotificationService):
        self.s, self.repo, self.notifications = settings, repo, notifications
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def schedule(self, *, appointment_id: str, patient_name: str, patient_phone: str, doctor_name: str,
                 date: str, start_time: str):
        """Create the reminder row (idempotent). Returns the row or None if reminders are disabled."""
        if not self.s.reminders_enabled:
            return None
        start = appointment_start_ist(date, start_time)
        offset = self.s.reminder_offset_hours * 60
        remind_ist = start - timedelta(minutes=offset)
        status = "pending"
        error = None
        if remind_ist <= now_ist():  # booked too close to the appointment: confirmation SMS already covers it
            status, error = "skipped", "reminder time already passed when booked"
        row, created = self.repo.create_if_absent(
            appointment_id=appointment_id, offset_minutes=offset, remind_at=to_utc_naive(remind_ist),
            status=status, last_error=error, patient_name=patient_name, patient_phone=patient_phone,
            doctor_name=doctor_name, date=date, start_time=start_time)
        if created:
            log.info("reminder scheduled appointment=%s status=%s", appointment_id, status)
        else:
            log.info("reminder already exists appointment=%s (duplicate prevented)", appointment_id)
        return row

    def cancel(self, appointment_id: str) -> int:
        n = self.repo.cancel_for_appointment(appointment_id)
        log.info("cancelled %d pending reminder(s) for appointment=%s", n, appointment_id)
        return n

    def list_for(self, appointment_id: str):
        return self.repo.list_for_appointment(appointment_id)

    async def run_due(self, now: datetime | None = None) -> int:
        """Send every due reminder once. Returns number sent."""
        now = now or utcnow()
        sent = 0
        for r in self.repo.due(now):
            if not self.repo.claim(r.id):
                continue  # another worker took it
            start_utc = to_utc_naive(appointment_start_ist(r.date, r.start_time))
            if start_utc <= now:
                self.repo.finish(r.id, "skipped", "appointment time already passed")
                continue
            res = await self.notifications.send_reminder(
                reminder_id=r.id, appointment_id=r.appointment_id, patient_name=r.patient_name,
                patient_phone=r.patient_phone, doctor_name=r.doctor_name, date=r.date, start_time=r.start_time)
            if res.sent or res.duplicate:
                self.repo.finish(r.id, "sent")
                sent += 1
            elif r.attempts + 1 >= MAX_ATTEMPTS:
                self.repo.finish(r.id, "failed", res.error)
            else:  # retry later; SMS send is a safe-to-retry notification (dedupe key blocks double sends)
                self.repo.finish(r.id, "pending", res.error, retry_at=now + RETRY_DELAY)
        return sent

    # ---- background loop ----
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("reminder loop error")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.reminder_poll_seconds)

    def start(self) -> None:
        recovered = self.repo.recover_stuck()
        if recovered:
            log.info("recovered %d reminder(s) interrupted by a restart", recovered)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
