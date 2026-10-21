from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.security import require_internal_key

router = APIRouter(prefix="/api", tags=["summaries"], dependencies=[Depends(require_internal_key)])


@router.get("/calls/{call_id}/summary")
async def call_summary(call_id: str, request: Request):
    c = request.app.state.container
    row = c.calls.get(call_id)
    if row is None:
        raise HTTPException(404, "Unknown call")
    stored = c.calls.get_summary(call_id)
    return stored if stored else c.summaries.build(call_id, final=False).model_dump()


@router.get("/appointments/{appointment_id}/reminders")
async def reminders(appointment_id: str, request: Request):
    c = request.app.state.container
    rows = c.reminders.list_for(appointment_id)
    return [{"id": r.id, "status": r.status, "remind_at_utc": r.remind_at.isoformat(), "attempts": r.attempts,
             "appointment_date": r.date, "appointment_time": r.start_time} for r in rows]
