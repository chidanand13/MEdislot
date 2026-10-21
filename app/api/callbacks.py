from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schemas import CallbackCreate
from app.security import require_internal_key
from app.utils import normalize_phone

router = APIRouter(prefix="/api/callbacks", tags=["callbacks"], dependencies=[Depends(require_internal_key)])


def _out(r) -> dict:
    return {"id": r.id, "call_id": r.call_id, "reason": r.reason, "caller_number": r.caller_number,
            "patient_name": r.patient_name, "notes": r.notes, "status": r.status,
            "created_at": r.created_at.isoformat()}


@router.post("", status_code=201)
async def create_callback(body: CallbackCreate, request: Request):
    c = request.app.state.container
    number = None
    if body.caller_number:
        try:
            number = normalize_phone(body.caller_number)
        except ValueError:
            raise HTTPException(422, "caller_number is not a valid phone number")
    res = await c.handoff.request(body.reason, body.call_id, number, body.patient_name, body.notes)
    return {"callback_id": res["callback_id"], "created": res["created"]}


@router.get("")
async def list_callbacks(request: Request, status: str | None = None):
    return [_out(r) for r in request.app.state.container.callbacks.list(status)]
