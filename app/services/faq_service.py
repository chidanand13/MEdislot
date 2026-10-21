"""Approved-FAQ lookup. The model may only answer hospital questions from this data."""
from __future__ import annotations

import json
import re
from pathlib import Path


class FaqService:
    def __init__(self, path: str):
        self.path = Path(path)
        self._faqs: list[dict] = []
        self.reload()

    def reload(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._faqs = list(data.get("faqs", []))
        except (OSError, ValueError):
            self._faqs = []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z]+", text.lower()))

    def lookup(self, question: str, max_results: int = 2) -> dict:
        tokens = self._tokens(question)
        scored = []
        for f in self._faqs:
            kw = {k.lower() for k in f.get("keywords", [])}
            score = len(tokens & kw)
            if score:
                scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return {"found": False,
                    "message": "No approved answer exists. Do NOT guess. Offer a staff callback."}
        return {"found": True, "answers": [
            {"topic": f["topic"], "answer": f["answer"], "verified": f.get("verified", False)}
            for _, f in scored[:max_results]]}
