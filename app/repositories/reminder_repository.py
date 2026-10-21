from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.db_models import ReminderRow
from app.utils import utcnow


class ReminderRepository:
    def __init__(self, sf: sessionmaker):
        self.sf = sf

    def create_if_absent(self, **fields) -> tuple[ReminderRow, bool]:
        row = ReminderRow(**fields)
        try:
            with self.sf() as s, s.begin():
                s.add(row)
            return row, True
        except IntegrityError:  # unique(appointment_id, offset_minutes) => no duplicates
            with self.sf() as s:
                existing = s.scalar(select(ReminderRow).where(
                    ReminderRow.appointment_id == fields["appointment_id"],
                    ReminderRow.offset_minutes == fields["offset_minutes"]))
            return existing, False

    def list_for_appointment(self, appointment_id: str) -> list[ReminderRow]:
        with self.sf() as s:
            return list(s.scalars(select(ReminderRow).where(
                ReminderRow.appointment_id == appointment_id).order_by(ReminderRow.id)))

    def due(self, now: datetime, limit: int = 50) -> list[ReminderRow]:
        with self.sf() as s:
            return list(s.scalars(select(ReminderRow).where(
                ReminderRow.status == "pending", ReminderRow.remind_at <= now).limit(limit)))

    def claim(self, reminder_id: int) -> bool:
        """Atomically move pending -> sending. Only one worker can win."""
        with self.sf() as s, s.begin():
            res = s.execute(update(ReminderRow).where(
                ReminderRow.id == reminder_id, ReminderRow.status == "pending"
            ).values(status="sending", attempts=ReminderRow.attempts + 1))
            return res.rowcount == 1

    def finish(self, reminder_id: int, status: str, error: str | None = None,
               retry_at: datetime | None = None) -> None:
        with self.sf() as s, s.begin():
            row = s.get(ReminderRow, reminder_id)
            if row is None:
                return
            row.status = status
            row.last_error = error[:300] if error else None
            if status == "sent":
                row.sent_at = utcnow()
            if retry_at is not None:
                row.remind_at = retry_at

    def cancel_for_appointment(self, appointment_id: str) -> int:
        with self.sf() as s, s.begin():
            res = s.execute(update(ReminderRow).where(
                ReminderRow.appointment_id == appointment_id, ReminderRow.status == "pending"
            ).values(status="cancelled"))
            return res.rowcount

    def recover_stuck(self) -> int:
        """After a crash, 'sending' rows go back to pending (at-least-once; notification dedupe guards repeats)."""
        with self.sf() as s, s.begin():
            res = s.execute(update(ReminderRow).where(ReminderRow.status == "sending").values(status="pending"))
            return res.rowcount
