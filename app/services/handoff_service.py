"""Human handoff / callback fallback. Saves a callback locally and alerts staff (no HMS endpoint is used)."""
from __future__ import annotations

import logging

from app.repositories.callback_repository import CallbackRepository
from app.services.notification_service import NotificationService
from app.utils import mask_phone

log = logging.getLogger(__name__)


class HandoffService:
    def __init__(self, repo: CallbackRepository, notifications: NotificationService):
        self.repo, self.notifications = repo, notifications

    async def request(self, reason: str, call_id: str | None = None, caller_number: str | None = None,
                      patient_name: str | None = None, notes: str | None = None) -> dict:
        row, created = self.repo.create(reason, call_id, caller_number, patient_name, notes)
        if created:
            body = (f"Callback #{row.id} requested.\nReason: {reason}\nCall: {call_id or '-'}\n"
                    f"Caller: {mask_phone(caller_number) or 'unknown'} (full number via GET /api/callbacks)")
            await self.notifications.alert_staff("callback", f"Callback required (#{row.id})", body,
                                                 dedupe_key=f"callback:{row.id}")
        return {"callback_id": row.id, "created": created}
