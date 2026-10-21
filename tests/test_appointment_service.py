import httpx
import pytest

from tests.conftest import patient_sms, HMS, appt_payload, future_date, slots_payload

BOOK = dict(doctor_id="DOC-001", start_time="10:00", patient_name="Asha Verma", patient_phone="+919876543210")


@pytest.fixture
def svc(make_container):
    c = make_container()
    return c


async def test_confirmed_booking_sends_confirmation_and_schedules_reminder(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=d)))
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "confirmed" and res["appointment_id"] == "APT-261019-5YUQEO"
    assert len(patient_sms(sms)) == 1 and "APT-261019-5YUQEO" in sms.sent[0]["body"]
    rem = svc.reminders.list_for("APT-261019-5YUQEO")
    assert len(rem) == 1 and rem[0].status == "pending"


async def test_pending_booking_sends_no_confirmation(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=d, status="pending")))
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "pending"
    assert patient_sms(sms) == [] and svc.reminders.list_for("APT-261019-5YUQEO") == []


async def test_slot_conflict_returns_alternatives_and_no_confirmation(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(409, json={"success": False, "message": "Slot already booked"}))
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(d, slots=("10:15", "10:30"))))
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "conflict" and res["alternatives"] == ["10:15", "10:30"]
    assert patient_sms(sms) == []


async def test_timeout_is_uncertain_not_retried_and_blocks_duplicate(svc, mock_hms, sms, email):
    d = future_date(5)
    route = mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=httpx.ReadTimeout("slow"))
    first = await svc.appointments.book(date=d, **BOOK)
    assert first["status"] == "uncertain" and first["handoff_needed"] is True
    second = await svc.appointments.book(date=d, **BOOK)  # must NOT hit HMS again
    assert second["status"] == "uncertain"
    assert route.call_count == 1
    assert patient_sms(sms) == []
    assert any("uncertain" in m["subject"].lower() for m in email.sent)  # staff alerted


async def test_duplicate_confirmed_booking_not_resubmitted(svc, mock_hms):
    d = future_date(5)
    route = mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=d)))
    await svc.appointments.book(date=d, **BOOK)
    again = await svc.appointments.book(date=d, **BOOK)
    assert again["status"] == "duplicate" and route.call_count == 1


async def test_2xx_without_id_is_uncertain_never_confirmed(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(200, json={"success": True}))
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "uncertain" and patient_sms(sms) == []


async def test_api_unreachable_fails_safely(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=httpx.ConnectError("down"))
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "failed" and res["api_unavailable"] and patient_sms(sms) == []


async def test_retry_after_conflict_uses_new_idempotency_key(svc, mock_hms):
    d = future_date(5)
    route = mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=[
        httpx.Response(409, json={"message": "taken"}), httpx.Response(201, json=appt_payload(date=d))])
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(d)))
    await svc.appointments.book(date=d, **BOOK)
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "confirmed"
    keys = [c.request.headers["Idempotency-Key"] for c in route.calls]
    assert keys[0] != keys[1]


async def test_availability_doctor_on_leave(svc, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(422, json={"success": False, "message": "Doctor is on leave"}))
    res = await svc.appointments.check_availability(future_date(), doctor_id="DOC-001")
    assert res["status"] == "doctor_on_leave"


async def test_availability_no_slots_and_past_date(svc, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(slots=())))
    assert (await svc.appointments.check_availability(future_date(), doctor_id="DOC-001"))["status"] == "no_slots"
    assert (await svc.appointments.check_availability("2020-01-01", doctor_id="DOC-001"))["status"] == "invalid_input"


async def _book_confirmed(svc, mock_hms, d):
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=d)))
    await svc.appointments.book(date=d, **BOOK)


async def test_cancellation_success_cancels_reminders(svc, mock_hms):
    d = future_date(5)
    await _book_confirmed(svc, mock_hms, d)
    mock_hms.patch(f"{HMS}/hms/appointments/APT-261019-5YUQEO").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"id": "APT-261019-5YUQEO", "status": "cancelled"}}))
    res = await svc.appointments.cancel("APT-261019-5YUQEO")
    assert res["status"] == "cancelled"
    assert svc.reminders.list_for("APT-261019-5YUQEO")[0].status == "cancelled"


async def test_failed_cancellation_keeps_reminder(svc, mock_hms):
    d = future_date(5)
    await _book_confirmed(svc, mock_hms, d)
    mock_hms.patch(f"{HMS}/hms/appointments/APT-261019-5YUQEO").mock(return_value=httpx.Response(404, json={"message": "Appointment not found"}))
    res = await svc.appointments.cancel("APT-261019-5YUQEO")
    assert res["status"] == "not_found"
    assert svc.reminders.list_for("APT-261019-5YUQEO")[0].status == "pending"
    mock_hms.patch(f"{HMS}/hms/appointments/APT-261019-5YUQEO").mock(side_effect=httpx.ReadTimeout("x"))
    assert (await svc.appointments.cancel("APT-261019-5YUQEO"))["status"] == "uncertain"


async def test_conflict_uses_backend_alternatives_without_extra_call(svc, mock_hms, sms):
    d = future_date(5)
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(409, json={
        "success": False, "error": {"code": "SLOT_TAKEN", "message": "x", "details": {"alternatives": ["10:15", "11:00"]}}}))
    avail = mock_hms.get(f"{HMS}/hms/availability")
    res = await svc.appointments.book(date=d, **BOOK)
    assert res["status"] == "conflict" and res["alternatives"] == ["10:15", "11:00"] and avail.call_count == 0


async def test_leave_detected_by_error_code(svc, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(422, json={
        "success": False, "error": {"code": "DOCTOR_ON_LEAVE", "message": "Doctor unavailable on this date"}}))
    assert (await svc.appointments.check_availability(future_date(), doctor_id="DOC-004"))["status"] == "doctor_on_leave"
