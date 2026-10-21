from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.db_models import CallbackRow


class CallbackRepository:
    def __init__(self, sf: sessionmaker):
        self.sf = sf

    def create(self, reason: str, call_id: str | None = None, caller_number: str | None = None,
               patient_name: str | None = None, notes: str | None = None) -> tuple[CallbackRow, bool]:
        """Returns (row, created). One callback per (call_id, reason)."""
        row = CallbackRow(call_id=call_id, reason=reason[:300], caller_number=caller_number,
                          patient_name=patient_name, notes=(notes or None) and notes[:500])
        try:
            with self.sf() as s, s.begin():
                s.add(row)
            return row, True
        except IntegrityError:
            with self.sf() as s:
                existing = s.scalar(select(CallbackRow).where(
                    CallbackRow.call_id == call_id, CallbackRow.reason == reason[:300]))
            return existing, False

    def list(self, status: str | None = None, limit: int = 100) -> list[CallbackRow]:
        with self.sf() as s:
            q = select(CallbackRow).order_by(CallbackRow.id.desc()).limit(limit)
            if status:
                q = q.where(CallbackRow.status == status)
            return list(s.scalars(q))

    def for_call(self, call_id: str) -> list[CallbackRow]:
        with self.sf() as s:
            return list(s.scalars(select(CallbackRow).where(CallbackRow.call_id == call_id)))
