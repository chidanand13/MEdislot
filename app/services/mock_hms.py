"""LOCAL FICTIONAL HMS used ONLY in mock mode (no HMS_API_KEY). It mimics the documented
contract using an httpx MockTransport so the real HMSClient code path is exercised.
It is NOT Akhil's backend and must never be used in production."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import httpx

MOCK_BASE_URL = "https://mock-hms.invalid/webhook"
DOCTORS = [
    {"id": "DOC-001", "name": "Dr. Ananya Rao", "specialty": "Pediatrics"},
    {"id": "DOC-002", "name": "Dr. Rahul Menon", "specialty": "Cardiology"},
    {"id": "DOC-003", "name": "Dr. Sneha Iyer", "specialty": "Dermatology"},
]
SLOTS = ["10:00", "10:15", "10:30", "11:00", "11:15"]


def build_mock_transport() -> httpx.MockTransport:
    booked: dict[tuple[str, str, str], dict] = {}
    appts: dict[str, dict] = {}

    def on_leave(doctor_id: str, date: str) -> bool:
        # Dr. Sneha Iyer is on leave on Mondays (fictional rule)
        return doctor_id == "DOC-003" and datetime.strptime(date, "%Y-%m-%d").weekday() == 0

    def free_slots(doctor_id: str, date: str) -> list[str]:
        return [s for s in SLOTS if (doctor_id, date, s) not in booked]

    def respond(status: int, body: dict) -> httpx.Response:
        return httpx.Response(status, json=body)

    def handler(request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path.endswith("/hms/doctors") and request.method == "GET":
            docs = [d for d in DOCTORS if not q.get("specialty") or d["specialty"].lower() == q["specialty"].lower()]
            return respond(200, {"success": True, "data": {"doctors": docs,
                                                          "specialties": sorted({d["specialty"] for d in DOCTORS})}})
        if path.endswith("/hms/availability") and request.method == "GET":
            date = q.get("date", "")
            if q.get("doctor_id"):
                doc = next((d for d in DOCTORS if d["id"] == q["doctor_id"]), None)
                if doc is None:
                    return respond(404, {"success": False, "message": "Doctor not found"})
                if on_leave(doc["id"], date):
                    return respond(422, {"success": False, "message": "Doctor is on leave on this date"})
                return respond(200, {"success": True, "data": {"doctor_id": doc["id"], "doctor_name": doc["name"],
                                                              "date": date, "slots": free_slots(doc["id"], date)}})
            spec = (q.get("specialty") or "").lower()
            out = [{"doctor_id": d["id"], "doctor_name": d["name"], "specialty": d["specialty"], "date": date,
                    "slots": free_slots(d["id"], date)}
                   for d in DOCTORS if d["specialty"].lower() == spec and not on_leave(d["id"], date)]
            return respond(200, {"success": True, "data": {"doctors": out}})
        if path.endswith("/hms/appointments") and request.method == "POST":
            b = json.loads(request.content)
            doc = next((d for d in DOCTORS if d["id"] == b.get("doctor_id")), None)
            if doc is None:
                return respond(404, {"success": False, "message": "Doctor not found"})
            key = (doc["id"], b["date"], b["start_time"])
            if key in booked or b["start_time"] not in SLOTS:
                return respond(409, {"success": False, "message": "Slot is no longer available"})
            h, m = map(int, b["start_time"].split(":"))
            end = f"{(h * 60 + m + 15) // 60:02d}:{(h * 60 + m + 15) % 60:02d}"
            appt = {"id": "APT-MOCK-" + uuid.uuid4().hex[:6].upper(), "doctor_id": doc["id"],
                    "doctor_name": doc["name"], "patient_name": b["patient_name"],
                    "patient_phone": b["patient_phone"].replace(" ", ""), "date": b["date"],
                    "start_time": b["start_time"], "end_time": end, "status": b.get("status", "confirmed")}
            booked[key] = appt
            appts[appt["id"]] = appt
            return respond(201, {"success": True, "data": appt})
        if "/hms/appointments/" in path and request.method == "PATCH":
            appt = appts.get(path.rsplit("/", 1)[-1])
            if appt is None:
                return respond(404, {"success": False, "message": "Appointment not found"})
            appt["status"] = "cancelled"
            booked.pop((appt["doctor_id"], appt["date"], appt["start_time"]), None)
            return respond(200, {"success": True, "data": appt})
        return respond(404, {"success": False, "message": "Not found"})

    return httpx.MockTransport(handler)
