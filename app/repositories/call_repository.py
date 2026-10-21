from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.db_models import CallRow
from app.models.schemas import SessionState
from app.utils import utcnow


class CallRepository:
    def __init__(self, sf: sessionmaker):
        self.sf = sf

    def create(self, channel: str, caller_number: str | None = None,
               provider_call_sid: str | None = None, state: SessionState | None = None) -> CallRow:
        row = CallRow(id=uuid.uuid4().hex, channel=channel, caller_number=caller_number,
                      provider_call_sid=provider_call_sid,
                      state_json=(state or SessionState()).model_dump_json())
        with self.sf() as s, s.begin():
            s.add(row)
        return row

    def get(self, call_id: str) -> CallRow | None:
        with self.sf() as s:
            return s.get(CallRow, call_id)

    def get_by_sid(self, sid: str) -> CallRow | None:
        with self.sf() as s:
            return s.scalar(select(CallRow).where(CallRow.provider_call_sid == sid))

    def load_state(self, call_id: str) -> SessionState:
        row = self.get(call_id)
        if row is None:
            raise KeyError(call_id)
        return SessionState.model_validate_json(row.state_json or "{}")

    def save_state(self, call_id: str, state: SessionState) -> None:
        with self.sf() as s, s.begin():
            row = s.get(CallRow, call_id)
            if row is None:
                raise KeyError(call_id)
            row.state_json = state.model_dump_json()

    def complete(self, call_id: str, summary: dict, purge_transcript: bool, state: SessionState) -> None:
        with self.sf() as s, s.begin():
            row = s.get(CallRow, call_id)
            if row is None:
                raise KeyError(call_id)
            row.status = "completed"
            row.ended_at = utcnow()
            row.summary_json = json.dumps(summary)
            if purge_transcript:
                state.messages = []
                state.pending_booking = None
                state.pending_cancel = None
            row.state_json = state.model_dump_json()

    def mark_summary_sent(self, call_id: str) -> None:
        with self.sf() as s, s.begin():
            row = s.get(CallRow, call_id)
            if row:
                row.summary_sent = True

    def get_summary(self, call_id: str) -> dict | None:
        row = self.get(call_id)
        return json.loads(row.summary_json) if row and row.summary_json else None
