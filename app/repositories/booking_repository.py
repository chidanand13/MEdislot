from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.db_models import BookingOperationRow


class BookingRepository:
    def __init__(self, sf: sessionmaker):
        self.sf = sf

    def get_by_key(self, dedupe_key: str) -> BookingOperationRow | None:
        with self.sf() as s:
            return s.scalar(select(BookingOperationRow).where(BookingOperationRow.dedupe_key == dedupe_key))

    def get_by_appointment(self, appointment_id: str) -> BookingOperationRow | None:
        with self.sf() as s:
            return s.scalar(select(BookingOperationRow).where(BookingOperationRow.appointment_id == appointment_id))

    def begin_operation(self, dedupe_key: str, **fields) -> BookingOperationRow:
        """Create or reset (for conflict/failed retries) the operation row; returns it with a fresh idempotency key."""
        with self.sf() as s, s.begin():
            old = s.scalar(select(BookingOperationRow).where(BookingOperationRow.dedupe_key == dedupe_key))
            if old is not None:  # conflict/failed retry: replace row so a NEW idempotency key is used
                s.delete(old)
                s.flush()
            row = BookingOperationRow(id=uuid.uuid4().hex, dedupe_key=dedupe_key, status="in_flight", **fields)
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def update(self, op_id: str, **fields) -> None:
        with self.sf() as s, s.begin():
            row = s.get(BookingOperationRow, op_id)
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
