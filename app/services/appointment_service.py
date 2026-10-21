"""Business operations on top of the HMS client: availability, safe booking, verified cancellation.

Safety rules implemented here:
  * Booking is recorded as 'in_flight' BEFORE the POST, so a crash/timeout can never lead to a blind re-submit.
  * An 'uncertain' operation (timeout / 5xx) is never retried automatically; staff must verify.
  * A 'confirmed' duplicate returns the existing booking instead of posting again.
  * Confirmation SMS + reminders happen ONLY after the HMS reports a confirmed booking.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.config import Settings
from app.repositories.booking_repository import BookingRepository
from app.services.hms_api import HMSClient, HMSHTTPError, HMSOutcomeUnknown, HMSUnavailable
from app.services.notification_service import NotificationService
from app.services.reminder_service import ReminderService
from app.utils import last4, today_ist, valid_date, mask_phone

import re
from datetime import datetime

log = logging.getLogger(__name__)
_CONFLICT_WORDS = re.compile(r"taken|conflict|already|booked|no longer|not available|unavailable", re.I)


class AppointmentService:
    def __init__(self, settings: Settings, hms: HMSClient, repo: BookingRepository,
                 notifications: NotificationService, reminders: ReminderService):
        self.s, self.hms, self.repo, self.notifications, self.reminders = settings, hms, repo, notifications, reminders

    # ------------------------------------------------------------------ read-only
    async def list_doctors(self, specialty: str | None = None) -> dict:
        try:
            dl = await self.hms.list_doctors(specialty)
        except HMSHTTPError as e:
            return {"status": "error", "message": e.message or f"HMS rejected the request ({e.status_code})"}
        except (HMSUnavailable, HMSOutcomeUnknown):
            return {"status": "api_unavailable", "message": "The hospital system is not reachable right now."}
        return {"status": "ok", "doctors": [d.model_dump() for d in dl.doctors], "specialties": dl.specialties}

    async def check_availability(self, date: str, doctor_id: str | None = None, specialty: str | None = None) -> dict:
        if not valid_date(date):
            return {"status": "invalid_input", "message": "date must be YYYY-MM-DD"}
        if datetime.strptime(date, "%Y-%m-%d").date() < today_ist():
            return {"status": "invalid_input", "message": "That date is in the past."}
        try:
            entries, payload = await self.hms.get_availability(date, doctor_id=doctor_id, specialty=specialty)
            if specialty and not doctor_id and entries and any(e.doctor_id is None for e in entries):
                entries = await self._per_doctor(specialty, date)  # backend did not return doctor ids
        except HMSHTTPError as e:
            msg = e.message
            if e.status_code == 422 and (e.code == "DOCTOR_ON_LEAVE" or re.search(r"leave", msg or "", re.I)):
                return {"status": "doctor_on_leave", "message": msg or "The doctor is on leave on that date."}
            if e.status_code == 404:
                return {"status": "not_found", "message": msg or "Doctor or specialty not found."}
            return {"status": "unavailable", "message": msg or f"No availability could be returned ({e.status_code})."}
        except (HMSUnavailable, HMSOutcomeUnknown):
            return {"status": "api_unavailable", "message": "The hospital system is not reachable right now."}

        results = [{"doctor_id": e.doctor_id, "doctor_name": e.doctor_name, "specialty": e.specialty or specialty,
                    "slots": e.slots} for e in entries if e.slots]
        out: dict[str, Any] = {"status": "ok" if results else "no_slots", "date": date, "results": results}
        alt = _find_alternatives(payload)
        if alt and not results:
            out["backend_alternatives"] = alt
        return out

    async def _per_doctor(self, specialty: str, date: str):
        dl = await self.hms.list_doctors(specialty)
        out = []
        for d in dl.doctors:
            try:
                entries, _ = await self.hms.get_availability(date, doctor_id=d.id)
            except HMSHTTPError:
                continue  # e.g. on leave
            for e in entries:
                e.doctor_id, e.doctor_name, e.specialty = e.doctor_id or d.id, e.doctor_name or d.name, d.specialty
                out.append(e)
        return out

    async def verify_slot(self, doctor_id: str, date: str, start_time: str) -> dict:
        """Live check used right before asking the caller to confirm."""
        res = await self.check_availability(date, doctor_id=doctor_id)
        if res["status"] != "ok":
            return res
        for r in res["results"]:
            if start_time in r["slots"]:
                return {"status": "available", "doctor_name": r["doctor_name"], "slots": r["slots"]}
        slots = res["results"][0]["slots"] if res["results"] else []
        return {"status": "slot_not_available", "alternatives": slots, "doctor_name": res["results"][0]["doctor_name"] if res["results"] else None}

    # ------------------------------------------------------------------ booking
    async def book(self, *, doctor_id: str, date: str, start_time: str, patient_name: str, patient_phone: str,
                   call_id: str | None = None) -> dict:
        key = hashlib.sha256(f"{patient_phone}|{doctor_id}|{date}|{start_time}".encode()).hexdigest()
        existing = self.repo.get_by_key(key)
        if existing is not None:
            if existing.status in ("confirmed", "pending"):
                return self._result("duplicate", existing, "This appointment was already booked; not booking it again.")
            if existing.status in ("in_flight", "uncertain"):
                return self._result("uncertain", existing,
                                    "An earlier booking attempt for this slot has an unknown outcome; staff must verify. "
                                    "It will not be submitted again automatically.", handoff=True)
        op = self.repo.begin_operation(key, call_id=call_id, doctor_id=doctor_id, date=date, start_time=start_time,
                                       patient_name=patient_name, patient_phone=patient_phone)
        body = {"doctor_id": doctor_id, "date": date, "start_time": start_time, "patient_name": patient_name,
                "patient_phone": patient_phone, "status": "confirmed"}
        try:
            payload = await self.hms.create_appointment(body, idempotency_key=op.id)
        except HMSUnavailable:
            self.repo.update(op.id, status="failed", error="HMS unreachable (request not delivered)")
            await self._alert_failure("api_unavailable", op.id, call_id)
            return self._result("failed", self.repo.get_by_key(key), "The hospital system could not be reached; nothing was booked.",
                                handoff=True, api_unavailable=True)
        except HMSOutcomeUnknown as e:
            self.repo.update(op.id, status="uncertain", error=str(e)[:290])
            await self._alert_failure("booking_uncertain", op.id, call_id)
            return self._result("uncertain", self.repo.get_by_key(key),
                                "The booking outcome could not be confirmed. It was NOT retried.", handoff=True)
        except HMSHTTPError as e:
            if e.status_code == 409 or e.code == "SLOT_TAKEN" or (e.status_code in (400, 422) and _CONFLICT_WORDS.search(e.message or "")):
                self.repo.update(op.id, status="conflict", error=(e.message or "slot taken")[:290])
                alts = e.alternatives or await self._alternatives(doctor_id, date)  # prefer the backend's own suggestions
                return self._result("conflict", self.repo.get_by_key(key), "That slot is no longer available.", alternatives=alts)
            self.repo.update(op.id, status="failed", error=(e.message or f"HTTP {e.status_code}")[:290])
            await self._alert_failure("booking_failed", op.id, call_id)
            return self._result("failed", self.repo.get_by_key(key), e.message or "The hospital system rejected the booking.", handoff=True)

        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("success") is False:
            msg = str(payload.get("message") or payload.get("error") or "")
            if _CONFLICT_WORDS.search(msg):
                self.repo.update(op.id, status="conflict", error=msg[:290])
                return self._result("conflict", self.repo.get_by_key(key), "That slot is no longer available.",
                                    alternatives=await self._alternatives(doctor_id, date))
            self.repo.update(op.id, status="failed", error=msg[:290])
            await self._alert_failure("booking_failed", op.id, call_id)
            return self._result("failed", self.repo.get_by_key(key), msg or "Booking was not accepted.", handoff=True)
        if not isinstance(data, dict) or not data.get("id"):
            # 2xx but no booking id: we cannot prove success -> treat as uncertain, never as confirmed.
            self.repo.update(op.id, status="uncertain", error="2xx response without appointment id")
            await self._alert_failure("booking_uncertain", op.id, call_id)
            return self._result("uncertain", self.repo.get_by_key(key), "The booking response could not be verified.", handoff=True)

        hms_status = str(data.get("status", "")).lower()
        status = "confirmed" if hms_status == "confirmed" else "pending"
        self.repo.update(op.id, status=status, appointment_id=str(data["id"]), doctor_name=data.get("doctor_name"),
                         end_time=data.get("end_time"))
        row = self.repo.get_by_key(key)
        if status == "confirmed":
            await self._after_confirmed(row)
        return self._result(status, row, "")

    async def _after_confirmed(self, row) -> None:
        """Confirmation SMS + reminder; failures here never change the booking result."""
        try:
            self.reminders.schedule(appointment_id=row.appointment_id, patient_name=row.patient_name,
                                    patient_phone=row.patient_phone, doctor_name=row.doctor_name or row.doctor_id,
                                    date=row.date, start_time=row.start_time)
            await self.notifications.send_booking_confirmation(
                appointment_id=row.appointment_id, patient_name=row.patient_name, patient_phone=row.patient_phone,
                doctor_name=row.doctor_name or row.doctor_id, date=row.date, start_time=row.start_time)
        except Exception:  # noqa: BLE001
            log.exception("post-booking notification step failed")

    async def _alternatives(self, doctor_id: str, date: str) -> list[str]:
        try:
            res = await self.check_availability(date, doctor_id=doctor_id)
            return res["results"][0]["slots"] if res.get("results") else []
        except Exception:  # noqa: BLE001
            return []

    async def _alert_failure(self, kind: str, op_id: str, call_id: str | None) -> None:
        try:
            await self.notifications.alert_staff(
                kind, f"Booking issue: {kind}", f"Booking operation {op_id} (call {call_id or '-'}) needs staff attention. "
                "If the outcome is uncertain, verify in the HMS before re-booking.", dedupe_key=f"bookfail:{op_id}")
        except Exception:  # noqa: BLE001
            log.exception("staff alert failed")

    @staticmethod
    def _result(status: str, row, message: str, *, alternatives: list[str] | None = None,
                handoff: bool = False, api_unavailable: bool = False) -> dict:
        out: dict[str, Any] = {"status": status, "message": message, "handoff_needed": handoff,
                               "api_unavailable": api_unavailable}
        if row is not None and row.appointment_id:
            out.update({"appointment_id": row.appointment_id, "doctor_name": row.doctor_name, "date": row.date,
                        "start_time": row.start_time, "end_time": row.end_time})
        if alternatives is not None:
            out["alternatives"] = alternatives
        return out

    # ------------------------------------------------------------------ cancellation
    def verify_cancellation(self, appointment_id: str, patient_name: str, patient_phone: str) -> dict | None:
        """Only appointments booked through MediSlot can be verified locally (HMS has no lookup endpoint).
        Returns the booking row summary if name AND phone match, else None."""
        row = self.repo.get_by_appointment(appointment_id)
        if row is None or row.status not in ("confirmed", "pending"):
            return None
        if row.patient_phone != patient_phone or row.patient_name.casefold() != " ".join(patient_name.split()).casefold():
            return None
        return {"doctor_name": row.doctor_name, "date": row.date, "start_time": row.start_time}

    async def cancel(self, appointment_id: str) -> dict:
        try:
            payload = await self.hms.cancel_appointment(appointment_id)
        except HMSUnavailable:
            return {"status": "failed", "message": "The hospital system could not be reached; nothing was cancelled.",
                    "handoff_needed": True}
        except HMSOutcomeUnknown:
            return {"status": "uncertain", "message": "The cancellation outcome could not be confirmed.",
                    "handoff_needed": True}
        except HMSHTTPError as e:
            return {"status": "not_found" if e.status_code == 404 else "failed",
                    "message": e.message or f"The hospital system rejected the cancellation ({e.status_code}).",
                    "handoff_needed": e.status_code != 404}
        data = payload.get("data") if isinstance(payload, dict) else None
        ok = isinstance(payload, dict) and payload.get("success") is not False and (
            (isinstance(data, dict) and str(data.get("status", "")).lower() in ("cancelled", "canceled"))
            or payload.get("success") is True)
        if not ok:
            return {"status": "failed", "message": "The hospital system did not confirm the cancellation.",
                    "handoff_needed": True}
        row = self.repo.get_by_appointment(appointment_id)
        if row:
            self.repo.update(row.id, status="cancelled")
        self.reminders.cancel(appointment_id)
        return {"status": "cancelled", "appointment_id": appointment_id, "handoff_needed": False}


def _find_alternatives(payload: Any) -> Any:
    node = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    if isinstance(node, dict):
        for k in ("alternatives", "next_available", "next_available_date", "suggestions"):
            if node.get(k):
                return node[k]
    if isinstance(payload, dict):
        for k in ("alternatives", "next_available"):
            if payload.get(k):
                return payload[k]
    return None
