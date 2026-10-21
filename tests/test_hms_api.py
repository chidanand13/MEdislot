import httpx
import pytest

from app.services.hms_api import HMSClient, HMSHTTPError, HMSOutcomeUnknown, HMSUnavailable
from tests.conftest import HMS, appt_payload, future_date, slots_payload

DOCS = {"success": True, "data": {"doctors": [
    {"id": "DOC-001", "name": "Dr. Ananya Rao", "specialty": "Pediatrics"},
    {"id": "DOC-002", "name": "Dr. Rahul Menon", "specialty": "Cardiology"}],
    "specialties": ["Cardiology", "Pediatrics"]}}


@pytest.fixture
def client():
    c = HMSClient(HMS, "hms-secret", timeout=2, get_retries=2, backoff=0)
    yield c


async def test_list_doctors_sends_api_key_and_active_flag(client, mock_hms):
    route = mock_hms.get(f"{HMS}/hms/doctors").mock(return_value=httpx.Response(200, json=DOCS))
    res = await client.list_doctors()
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "hms-secret"
    assert req.headers["content-type"] == "application/json"
    assert req.url.params["active"] == "true"
    assert [d.id for d in res.doctors] == ["DOC-001", "DOC-002"]
    assert res.specialties == ["Cardiology", "Pediatrics"]


async def test_specialty_filter_query_param(client, mock_hms):
    route = mock_hms.get(f"{HMS}/hms/doctors").mock(return_value=httpx.Response(200, json=DOCS))
    await client.list_doctors("Pediatrics")
    assert route.calls[0].request.url.params["specialty"] == "Pediatrics"


async def test_availability_by_doctor_and_specialty(client, mock_hms):
    d = future_date()
    route = mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(d)))
    entries, _ = await client.get_availability(d, doctor_id="DOC-001")
    assert entries[0].slots == ["10:00", "10:15"]
    assert route.calls[0].request.url.params["doctor_id"] == "DOC-001"
    await client.get_availability(d, specialty="Pediatrics")
    assert route.calls[1].request.url.params["specialty"] == "Pediatrics"


async def test_availability_http_422_doctor_on_leave(client, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(422, json={"success": False, "message": "Doctor is on leave"}))
    with pytest.raises(HMSHTTPError) as e:
        await client.get_availability(future_date(), doctor_id="DOC-001")
    assert e.value.status_code == 422 and "leave" in e.value.message


async def test_get_retries_then_unavailable(client, mock_hms):
    route = mock_hms.get(f"{HMS}/hms/doctors").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(HMSUnavailable):
        await client.list_doctors()
    assert route.call_count == 3  # 1 try + 2 retries (GET only)


async def test_post_timeout_is_outcome_unknown_and_not_retried(client, mock_hms):
    route = mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(HMSOutcomeUnknown):
        await client.create_appointment({"doctor_id": "DOC-001"}, "key-1")
    assert route.call_count == 1


async def test_post_connect_error_is_unavailable(client, mock_hms):
    mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(HMSUnavailable):
        await client.create_appointment({}, "k")


async def test_post_5xx_is_outcome_unknown(client, mock_hms):
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(502))
    with pytest.raises(HMSOutcomeUnknown):
        await client.create_appointment({}, "k")


async def test_idempotency_header_sent(client, mock_hms):
    route = mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload()))
    await client.create_appointment({"doctor_id": "DOC-001"}, "abc-123")
    assert route.calls[0].request.headers["Idempotency-Key"] == "abc-123"


async def test_path_prefix_for_express_service(mock_hms):
    c = HMSClient("http://localhost:4000/api", "k", get_retries=0, path_prefix="")
    route = mock_hms.get("http://localhost:4000/api/doctors").mock(return_value=httpx.Response(200, json=DOCS))
    assert len((await c.list_doctors()).doctors) == 2 and route.call_count == 1


async def test_akhil_response_shapes_parse(client, mock_hms):
    d = future_date()
    by_specialty = {"success": True, "data": {"specialty": "Pediatrics", "date": d, "earliest_slot": {"doctor_id": "DOC-003", "start": "10:00"},
        "doctors": [{"doctor_id": "DOC-003", "doctor_name": "Dr. Priya Nair", "slots": [{"start": "10:00", "end": "10:30"}]},
                    {"doctor_id": "DOC-004", "doctor_name": "Dr. Arjun Kapoor", "unavailable_reason": "DOCTOR_ON_LEAVE", "slots": []}]}}
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=by_specialty))
    entries, _ = await client.get_availability(d, specialty="Pediatrics")
    assert entries[0].doctor_id == "DOC-003" and entries[0].slots == ["10:00"] and entries[1].slots == []
    doc = {"success": True, "data": {"doctor_id": "DOC-001", "doctor_name": "Dr. Ananya Rao", "slots": [
        {"start": "09:00", "end": "09:15", "status": "available"}, {"start": "09:15", "status": "booked"}]}}
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=doc))
    entries, _ = await client.get_availability(d, doctor_id="DOC-001")
    assert entries[0].slots == ["09:00"]


async def test_error_code_and_alternatives_parsed(client, mock_hms):
    body = {"success": False, "error": {"code": "SLOT_TAKEN", "message": "The selected time slot is already booked.",
                                        "details": {"alternatives": ["10:15", "10:30"]}}}
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(409, json=body))
    with pytest.raises(HMSHTTPError) as e:
        await client.create_appointment({}, "k")
    assert e.value.code == "SLOT_TAKEN" and e.value.alternatives == ["10:15", "10:30"] and "booked" in e.value.message
