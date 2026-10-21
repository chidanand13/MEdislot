"""HTTP client for the existing HMS (n8n) API. Contains NO conversation logic.

Error model (important for booking safety):
  HMSHTTPError       - server answered with a 4xx: the request was definitively rejected.
  HMSUnavailable     - could not connect / not configured: the request definitely did NOT reach the server.
  HMSOutcomeUnknown  - timeout, dropped connection or 5xx on a WRITE: the server may have processed it.
Writes (POST/PATCH) are never retried automatically.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.models.schemas import Doctor, DoctorAvailability, DoctorList

log = logging.getLogger(__name__)


class HMSError(Exception):
    pass


class HMSHTTPError(HMSError):
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HMS returned HTTP {status_code}")

    @property
    def code(self) -> str:
        """Machine code such as SLOT_TAKEN / DOCTOR_ON_LEAVE (body.error.code), if present."""
        if isinstance(self.body, dict):
            err = self.body.get("error")
            if isinstance(err, dict) and isinstance(err.get("code"), str):
                return err["code"]
            if isinstance(self.body.get("code"), str):
                return self.body["code"]
        return ""

    @property
    def alternatives(self) -> list[str]:
        """Alternative HH:mm slots the backend suggests on a 409 (body.error.details.alternatives)."""
        if isinstance(self.body, dict):
            err = self.body.get("error")
            det = err.get("details") if isinstance(err, dict) else None
            alts = det.get("alternatives") if isinstance(det, dict) else None
            if isinstance(alts, list):
                return [a[:5] for a in alts if isinstance(a, str) and _SLOT_RE.match(a)]
        return []

    @property
    def message(self) -> str:
        if isinstance(self.body, dict):
            for k in ("message", "error", "detail"):
                v = self.body.get(k)
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and isinstance(v.get("message"), str):
                    return v["message"]
        return ""


class HMSUnavailable(HMSError):
    pass


class HMSOutcomeUnknown(HMSError):
    pass


_SLOT_RE = re.compile(r"^(\d{2}:\d{2})")


class HMSClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0, get_retries: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None, backoff: float = 0.3,
                 path_prefix: str = "/hms"):
        self.base_url = base_url.rstrip("/")
        self.prefix = path_prefix.rstrip("/")
        self._api_key = api_key
        self.get_retries = get_retries
        self.backoff = backoff
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------- low-level -------------
    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:300]}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.get_retries + 1):
            try:
                resp = await self._client.get(url, params=params, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = HMSUnavailable(f"HMS unreachable: {type(exc).__name__}")
            else:
                log.info("HMS GET %s -> %s", path, resp.status_code)
                if resp.status_code >= 500:
                    last = HMSUnavailable(f"HMS server error {resp.status_code}")
                elif resp.status_code >= 400:
                    raise HMSHTTPError(resp.status_code, self._parse(resp))
                else:
                    return self._parse(resp)
            if attempt < self.get_retries:  # GETs are safe to retry
                await asyncio.sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    async def _write(self, method: str, path: str, payload: dict, idempotency_key: str | None = None) -> Any:
        """POST/PATCH: single attempt, no retries."""
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.request(method, url, json=payload, headers=self._headers(idempotency_key))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise HMSUnavailable(f"HMS unreachable: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:  # ReadTimeout, WriteTimeout, dropped connection, ...
            raise HMSOutcomeUnknown(f"outcome unknown: {type(exc).__name__}") from exc
        log.info("HMS %s %s -> %s", method, path, resp.status_code)
        if resp.status_code >= 500:
            raise HMSOutcomeUnknown(f"HMS server error {resp.status_code}")
        if resp.status_code >= 400:
            raise HMSHTTPError(resp.status_code, self._parse(resp))
        return self._parse(resp)

    # ------------- endpoints (as documented in the integration guide) -------------
    async def list_doctors(self, specialty: str | None = None) -> DoctorList:
        params: dict[str, Any] = {"active": "true"}
        if specialty:
            params["specialty"] = specialty
        return parse_doctors(await self._get(f"{self.prefix}/doctors", params))

    async def get_availability(self, date: str, doctor_id: str | None = None,
                               specialty: str | None = None) -> tuple[list[DoctorAvailability], Any]:
        params: dict[str, Any] = {"date": date}
        if doctor_id:
            params["doctor_id"] = doctor_id
        elif specialty:
            params["specialty"] = specialty
        payload = await self._get(f"{self.prefix}/availability", params)
        return parse_availability(payload, date), payload

    async def create_appointment(self, body: dict, idempotency_key: str) -> Any:
        return await self._write("POST", f"{self.prefix}/appointments", body, idempotency_key)

    async def cancel_appointment(self, appointment_id: str) -> Any:
        return await self._write("PATCH", f"{self.prefix}/appointments/{appointment_id}", {"status": "cancelled"})


# ---------------- tolerant response parsing ----------------
# The exact list/availability response shapes are not fully documented, so the parsers accept the
# common shapes. VALIDATE against the live service (see README "Validate the HMS contract").
def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def parse_doctors(payload: Any) -> DoctorList:
    data = _unwrap(payload)
    raw_doctors: list = []
    specialties: list[str] = []
    if isinstance(data, list):
        raw_doctors = data
    elif isinstance(data, dict):
        raw_doctors = data.get("doctors") or []
        specialties = [str(s) for s in (data.get("specialties") or [])]
    if isinstance(payload, dict) and not specialties:
        specialties = [str(s) for s in (payload.get("specialties") or [])]
    if isinstance(payload, dict) and not raw_doctors:
        raw_doctors = payload.get("doctors") or []
    doctors: list[Doctor] = []
    for d in raw_doctors:
        if not isinstance(d, dict):
            continue
        did = _first(d, "id", "doctor_id")
        name = _first(d, "name", "doctor_name", "full_name")
        if did and name:
            doctors.append(Doctor(id=str(did), name=str(name),
                                  specialty=_first(d, "specialty", "specialization", "department")))
    if not specialties:
        specialties = sorted({d.specialty for d in doctors if d.specialty})
    return DoctorList(doctors=doctors, specialties=specialties)


def _slots(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            m = _SLOT_RE.match(item)
            if m:
                out.append(m.group(1))
        elif isinstance(item, dict):
            if item.get("available") is False or str(item.get("status", "")).lower() in ("booked", "unavailable"):
                continue
            t = _first(item, "start_time", "time", "start")
            if isinstance(t, str) and _SLOT_RE.match(t):
                out.append(_SLOT_RE.match(t).group(1))
    return out


def parse_availability(payload: Any, date: str) -> list[DoctorAvailability]:
    results: list[DoctorAvailability] = []

    def visit(node: Any, inherited: dict) -> None:
        if isinstance(node, list):
            for n in node:
                visit(n, inherited)
        elif isinstance(node, dict):
            ctx = dict(inherited)
            did = _first(node, "doctor_id", "id")
            if did:
                ctx["doctor_id"] = str(did)
            name = _first(node, "doctor_name", "name")
            if name:
                ctx["doctor_name"] = str(name)
            spec = _first(node, "specialty", "specialization")
            if spec:
                ctx["specialty"] = str(spec)
            slots_raw = node.get("slots", node.get("available_slots"))
            if slots_raw is not None:
                results.append(DoctorAvailability(
                    doctor_id=ctx.get("doctor_id"), doctor_name=ctx.get("doctor_name"),
                    specialty=ctx.get("specialty"), date=str(node.get("date") or date), slots=_slots(slots_raw)))
                return
            for key in ("doctors", "availability", "results", "data"):
                if key in node:
                    visit(node[key], ctx)

    visit(payload, {})
    return results
