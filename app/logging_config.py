"""Structured (JSON) logging with secret and phone-number redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{8,}\d")


class RedactingJsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._secrets = [s for s in (secrets or []) if len(s) >= 4]

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return _PHONE_RE.sub("[phone]", text)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": self._redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = self._redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingJsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs full URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
