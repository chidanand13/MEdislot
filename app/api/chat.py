from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schemas import ChatRequest, ChatResponse
from app.security import require_internal_key
from app.utils import normalize_phone

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_internal_key)])


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    c = request.app.state.container
    caller = None
    if body.caller_number:
        try:
            caller = normalize_phone(body.caller_number)
        except ValueError:
            raise HTTPException(422, "caller_number is not a valid phone number")
    if body.session_id:
        row = c.calls.get(body.session_id)
        if row is None:
            raise HTTPException(404, "Unknown session_id")
        if row.status == "completed":
            raise HTTPException(409, "Session already ended")
        session_id = row.id
    else:
        session_id = c.calls.create("chat", caller_number=caller).id
    turn = await c.receptionist.handle_turn(session_id, body.message, caller)
    if turn.end_call:
        await c.summaries.finalize(session_id)
    return ChatResponse(session_id=session_id, reply=turn.reply, ended=turn.end_call,
                        handoff_requested=turn.handoff_requested)


@router.post("/chat/{session_id}/end")
async def end_chat(session_id: str, request: Request):
    c = request.app.state.container
    if c.calls.get(session_id) is None:
        raise HTTPException(404, "Unknown session_id")
    return (await c.summaries.finalize(session_id)).model_dump()
