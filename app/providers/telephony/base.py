"""Telephony adapter interface. Everything provider-specific lives behind this."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response


@dataclass
class InboundCall:
    call_sid: str
    from_number: str | None


@dataclass
class CallEvent:
    call_sid: str
    kind: str  # "speech" | "silence" | "status"
    speech_text: str = ""
    status: str = ""


@dataclass
class VoiceReply:
    say: str
    listen: bool = True
    hangup: bool = False
    transfer_to: str | None = None


COMPLETED_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


class TelephonyProvider(abc.ABC):
    name = "base"
    is_live = False

    @abc.abstractmethod
    async def read_params(self, request: Request) -> dict[str, Any]: ...

    @abc.abstractmethod
    def verify(self, request: Request, params: dict[str, Any]) -> bool:
        """Return True only if the request is authentically from the provider."""

    @abc.abstractmethod
    def parse_inbound(self, params: dict[str, Any]) -> InboundCall: ...

    @abc.abstractmethod
    def parse_event(self, params: dict[str, Any]) -> CallEvent: ...

    @abc.abstractmethod
    def render(self, reply: VoiceReply, action_url: str) -> Response: ...
