from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.db_models import NotificationRow


class NotificationRepository:
    def __init__(self, sf: sessionmaker):
        self.sf = sf

    def claim(self, dedupe_key: str, kind: str, channel: str, recipient_masked: str) -> bool:
        """True if THIS caller should send. False => already sent/in progress (duplicate)."""
        try:
            with self.sf() as s, s.begin():
                s.add(NotificationRow(dedupe_key=dedupe_key, kind=kind, channel=channel,
                                      recipient=recipient_masked, attempts=1))
            return True
        except IntegrityError:
            with self.sf() as s, s.begin():
                row = s.scalar(select(NotificationRow).where(NotificationRow.dedupe_key == dedupe_key))
                if row is not None and row.status == "failed":  # allow retry of failed sends
                    row.status = "sending"
                    row.attempts += 1
                    return True
            return False

    def finish(self, dedupe_key: str, ok: bool, message_id: str | None = None, error: str | None = None) -> None:
        with self.sf() as s, s.begin():
            row = s.scalar(select(NotificationRow).where(NotificationRow.dedupe_key == dedupe_key))
            if row:
                row.status = "sent" if ok else "failed"
                row.provider_message_id = message_id
                row.error = (error or None) and error[:300]

    def get(self, dedupe_key: str) -> NotificationRow | None:
        with self.sf() as s:
            return s.scalar(select(NotificationRow).where(NotificationRow.dedupe_key == dedupe_key))

    def list(self, limit: int = 100) -> list[NotificationRow]:
        with self.sf() as s:
            return list(s.scalars(select(NotificationRow).order_by(NotificationRow.id.desc()).limit(limit)))
