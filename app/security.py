"""Internal-endpoint authentication and a small in-memory rate limiter."""
from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


async def require_internal_key(request: Request) -> None:
    expected = request.app.state.container.settings.internal_api_key.get_secret_value()
    if not expected:
        raise HTTPException(503, "INTERNAL_API_KEY is not configured on the server")
    supplied = request.headers.get("X-Internal-Api-Key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Invalid or missing X-Internal-Api-Key")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client sliding window. Webhook paths are exempt (they are protected by signature checks).
    In-memory => per process. Use a gateway or Redis-based limiter when running several workers."""

    def __init__(self, app, limit_per_minute: int):
        super().__init__(app)
        self.limit = limit_per_minute
        self.hits: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path == "/health" or (path.startswith("/api/calls/") and request.method == "POST") or path == "/api/calls/inbound"
        if self.limit > 0 and not exempt:
            key = request.client.host if request.client else "unknown"
            now = time.monotonic()
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.limit:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            q.append(now)
        return await call_next(request)
