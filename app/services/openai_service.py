"""LLM access behind a tiny interface: real OpenAI client, or a scripted MOCK for keyless local runs."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class AssistantMessage:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function",
                                "function": {"name": t.name, "arguments": t.arguments}} for t in self.tool_calls]
        return d


class LLMClient(Protocol):
    is_mock: bool

    async def chat(self, messages: list[dict], tools: list[dict], tool_choice: str = "auto") -> AssistantMessage: ...


class OpenAIChatClient:
    is_mock = False

    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI  # imported lazily so mock mode needs no key
        self._client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(),
                                   timeout=settings.openai_timeout_seconds, max_retries=2)
        self.model = settings.openai_model

    async def chat(self, messages, tools, tool_choice="auto") -> AssistantMessage:
        from openai import OpenAIError
        try:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools, tool_choice=tool_choice, temperature=0.2)
        except OpenAIError as exc:
            raise LLMError(type(exc).__name__) from exc
        msg = resp.choices[0].message
        calls = [ToolCall(id=t.id, name=t.function.name, arguments=t.function.arguments)
                 for t in (msg.tool_calls or [])]
        return AssistantMessage(content=msg.content, tool_calls=calls)

    async def aclose(self) -> None:
        await self._client.close()


class MockLLMClient:
    """Deterministic keyword 'model' for demos WITHOUT an OpenAI key. It supports greetings, FAQ lookup,
    doctor listing and availability only. Booking/cancellation dialogue needs a real model."""
    is_mock = True

    async def chat(self, messages, tools, tool_choice="auto") -> AssistantMessage:
        last = messages[-1]
        if last["role"] == "tool":
            return AssistantMessage(content=self._render(json.loads(last["content"])))
        text = (last.get("content") or "").lower()
        n = 0
        if re.search(r"\b(doctor|doctors|specialt)", text):
            return AssistantMessage(tool_calls=[ToolCall("m1", "list_doctors", "{}")])
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        spec = re.search(r"(pediatrics|cardiology|dermatology)", text)
        if m and spec:
            return AssistantMessage(tool_calls=[ToolCall("m2", "check_availability", json.dumps(
                {"specialty": spec.group(1).capitalize(), "date": m.group(1)}))])
        if re.search(r"\b(hours|open|location|address|department|contact|staff|how do i book|booking)\b", text):
            return AssistantMessage(tool_calls=[ToolCall("m3", "lookup_faq", json.dumps({"question": text[:200]}))])
        if re.search(r"\b(bye|goodbye|thank)", text):
            return AssistantMessage(content="You're welcome. Goodbye!")
        return AssistantMessage(content=(
            "[MOCK MODE] I can answer hospital FAQs, list doctors, and check availability "
            "(say e.g. 'availability for Pediatrics on 2026-10-30'). Full booking dialogue needs an OpenAI key. "
            "Say 'human' to request a staff callback."))

    @staticmethod
    def _render(r: dict) -> str:
        if r.get("doctors") is not None:
            names = ", ".join(f"{d['name']} ({d['specialty']})" for d in r["doctors"]) or "none"
            return f"Our active doctors are: {names}."
        if r.get("results") is not None:
            if not r["results"]:
                return "There are no free slots on that date."
            return " ".join(f"{x['doctor_name']}: {', '.join(x['slots'])}." for x in r["results"])
        if r.get("found"):
            return r["answers"][0]["answer"]
        if r.get("found") is False:
            return "I don't have an approved answer for that. I can request a staff callback if you like."
        return r.get("message") or "Done."


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.openai_api_key.get_secret_value():
        return OpenAIChatClient(settings)
    log.warning("OPENAI_API_KEY missing -> using scripted MOCK LLM (limited demo)")
    return MockLLMClient()
