"""Telephony webhooks. Authenticity is proven by the provider's signature, not the internal API key."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from app.providers.telephony.base import VoiceReply

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/calls", tags=["voice"])


def _base(request: Request) -> str:
    return (request.app.state.container.settings.public_base_url or str(request.base_url)).rstrip("/")


@router.post("/inbound")
async def inbound(request: Request):
    c = request.app.state.container
    tel = c.telephony
    params = await tel.read_params(request)
    if not tel.verify(request, params):
        log.warning("rejected inbound webhook: bad signature")
        raise HTTPException(403, "Invalid webhook signature")
    call_id = None
    try:
        call_id, reply = c.voice.start_call(tel.parse_inbound(params))
        return tel.render(reply, f"{_base(request)}/api/calls/{call_id}/events")
    except Exception:  # noqa: BLE001
        log.exception("inbound call setup failed")
        fb = await c.voice.error_fallback(call_id)
        return tel.render(fb, "")


@router.post("/status")
async def status(request: Request):
    """Provider 'call status changed' callback (no call_id in the URL)."""
    c = request.app.state.container
    tel = c.telephony
    params = await tel.read_params(request)
    if not tel.verify(request, params):
        raise HTTPException(403, "Invalid webhook signature")
    event = tel.parse_event(params)
    row = c.calls.get_by_sid(event.call_sid)
    if row is not None and event.kind == "status":
        await c.summaries.finalize(row.id)
    return Response(status_code=204)


@router.post("/{call_id}/events")
async def events(call_id: str, request: Request):
    c = request.app.state.container
    tel = c.telephony
    params = await tel.read_params(request)
    if not tel.verify(request, params):
        log.warning("rejected call event: bad signature")
        raise HTTPException(403, "Invalid webhook signature")
    if c.calls.get(call_id) is None:
        raise HTTPException(404, "Unknown call")
    event = tel.parse_event(params)
    try:
        reply = await c.voice.handle_event(call_id, event)
    except Exception:  # noqa: BLE001
        log.exception("call event handling failed")
        reply = await c.voice.error_fallback(call_id)
    if reply is None:
        return Response(status_code=204)
    return tel.render(reply, f"{_base(request)}/api/calls/{call_id}/events")
