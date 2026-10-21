import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.telephony.twilio import TwilioTelephonyProvider, compute_twilio_signature
from tests.conftest import AUTH, FakeLLM, HMS, appt_payload, future_date, make_settings, say, tc


@pytest.fixture
def client(make_container, settings):
    c = make_container()
    with TestClient(create_app(settings, c)) as tc_:
        yield tc_, c


def test_health_open_and_reports_modes(client):
    tcl, _ = client
    r = tcl.get("/health")
    assert r.status_code == 200 and r.json()["prototype"] is True


@pytest.mark.parametrize("method,path", [("post", "/api/chat"), ("get", "/api/callbacks"), ("post", "/api/callbacks"),
                                         ("get", "/api/calls/x/summary"), ("get", "/api/appointments/A/reminders")])
def test_internal_endpoints_require_key(client, method, path):
    tcl, _ = client
    assert getattr(tcl, method)(path).status_code == 401
    assert getattr(tcl, method)(path, headers={"X-Internal-Api-Key": "wrong"}).status_code == 401


def test_internal_key_not_configured_returns_503(tmp_path, make_container):
    s = make_settings(tmp_path, internal_api_key="")
    with TestClient(create_app(s, make_container(settings=s))) as tcl:
        assert tcl.get("/api/callbacks", headers=AUTH).status_code == 503


def test_chat_endpoint_and_summary(make_container, settings):
    c = make_container(FakeLLM([tc("lookup_faq", {"question": "opening hours"}), say("Open 9 to 6 (sample)."), say("Bye")]))
    with TestClient(create_app(settings, c)) as tcl:
        r = tcl.post("/api/chat", json={"message": "what are your opening hours"}, headers=AUTH)
        assert r.status_code == 200 and "Open" in r.json()["reply"]
        sid = r.json()["session_id"]
        s = tcl.get(f"/api/calls/{sid}/summary", headers=AUTH).json()
        assert s["caller_intent"] == "hospital_information" and s["final"] is False
        end = tcl.post(f"/api/chat/{sid}/end", headers=AUTH).json()
        assert end["final"] is True
        assert tcl.post("/api/chat", json={"session_id": sid, "message": "hi"}, headers=AUTH).status_code == 409


def test_chat_validation(client):
    tcl, _ = client
    assert tcl.post("/api/chat", json={"message": ""}, headers=AUTH).status_code == 422
    assert tcl.post("/api/chat", json={"message": "hi", "session_id": "nope"}, headers=AUTH).status_code == 404


def test_callbacks_create_list_dedupe(client, ):
    tcl, c = client
    body = {"reason": "Wants to discuss billing", "caller_number": "9876543210", "call_id": "call-1"}
    assert tcl.post("/api/callbacks", json=body, headers=AUTH).json()["created"] is True
    assert tcl.post("/api/callbacks", json=body, headers=AUTH).json()["created"] is False
    rows = tcl.get("/api/callbacks", headers=AUTH).json()
    assert len(rows) == 1 and rows[0]["caller_number"] == "+919876543210"
    assert tcl.post("/api/callbacks", json={**body, "caller_number": "12"}, headers=AUTH).status_code == 422


def test_reminders_endpoint(client):
    tcl, c = client
    c.reminders.schedule(appointment_id="APT-9", patient_name="A B", patient_phone="+919876543210", doctor_name="Dr",
                         date=future_date(5), start_time="10:00")
    r = tcl.get("/api/appointments/APT-9/reminders", headers=AUTH).json()
    assert len(r) == 1 and r[0]["status"] == "pending"


# ---------------- mock telephony ----------------
def test_mock_voice_call_flow(make_container, settings):
    c = make_container(FakeLLM([tc("lookup_faq", {"question": "location"}), say("We are at 12 Example Road (sample)."),
                                say("Goodbye!")]))
    with TestClient(create_app(settings, c)) as tcl:
        r = tcl.post("/api/calls/inbound", json={"call_sid": "CA1", "from": "+919876543210"}).json()
        assert "virtual AI receptionist" in r["say"] and r["mode"] == "mock"
        url = r["next_url"].replace("http://testserver", "")
        r2 = tcl.post(url, json={"call_sid": "CA1", "speech": "where are you located"}).json()
        assert "Example Road" in r2["say"] and r2["listen"] is True
        assert tcl.post(url, json={"call_sid": "CA1", "status": "completed"}).status_code == 204
        call = c.calls.get_by_sid("CA1")
        assert c.calls.get_summary(call.id)["caller_intent"] == "hospital_information"


def test_voice_unknown_call_404(client):
    tcl, _ = client
    assert tcl.post("/api/calls/doesnotexist/events", json={"speech": "hi"}).status_code == 404


def test_voice_error_falls_back_to_callback(make_container, settings):
    c = make_container(FakeLLM([RuntimeError("model exploded")]))
    with TestClient(create_app(settings, c)) as tcl:
        r = tcl.post("/api/calls/inbound", json={"call_sid": "CA2", "from": "+919876543210"}).json()
        url = r["next_url"].replace("http://testserver", "")
        r2 = tcl.post(url, json={"call_sid": "CA2", "speech": "hello"}).json()
        assert r2["hangup"] is True and "call you back" in r2["say"]
        assert len(c.callbacks.list()) == 1


# ---------------- Twilio signature verification ----------------
@pytest.fixture
def twilio_client(make_container, tmp_path):
    s = make_settings(tmp_path, telephony_provider="twilio", twilio_auth_token="tw-secret",
                      public_base_url="https://example.ngrok.app")
    c = make_container(FakeLLM([say("hi")]), settings=s, telephony=TwilioTelephonyProvider(s))
    with TestClient(create_app(s, c), base_url="https://example.ngrok.app") as tcl:
        yield tcl, c


def test_invalid_webhook_signature_rejected(twilio_client):
    tcl, c = twilio_client
    assert tcl.post("/api/calls/inbound", data={"CallSid": "CAx", "From": "+919876543210"}).status_code == 403
    assert tcl.post("/api/calls/inbound", data={"CallSid": "CAx"}, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    assert tcl.post("/api/calls/someid/events", data={"SpeechResult": "hi"}).status_code == 403
    assert c.calls.get_by_sid("CAx") is None  # no call session created by forged requests


def test_valid_signature_returns_twiml(twilio_client):
    tcl, c = twilio_client
    params = {"CallSid": "CAy", "From": "+919876543210"}
    sig = compute_twilio_signature("tw-secret", "https://example.ngrok.app/api/calls/inbound", params)
    r = tcl.post("/api/calls/inbound", data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<Gather" in r.text and "virtual AI receptionist" in r.text
    assert r.headers["content-type"].startswith("application/xml")


def test_twilio_reference_signature_vector():
    # Example from Twilio's documentation (validating requests)
    token = "12345"
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234", "From": "+14158675310", "To": "+18005551212"}
    assert compute_twilio_signature(token, url, params) == "GvWf1cFY/Q7PnoempGyD5oXAezc="


def test_rate_limit_returns_429(make_container, tmp_path):
    s = make_settings(tmp_path, rate_limit_per_minute=3)
    with TestClient(create_app(s, make_container(settings=s))) as tcl:
        codes = [tcl.get("/api/callbacks", headers=AUTH).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes


# ---------------- mock mode end-to-end (keyless) ----------------
def test_keyless_mock_mode_runs_end_to_end(tmp_path):
    s = make_settings(tmp_path, hms_api_key="", openai_api_key="")
    with TestClient(create_app(s)) as tcl:  # builds its own container: mock HMS + mock LLM + mock providers
        h = tcl.get("/health").json()["modes"]
        assert h["hms"] == "mock" and h["llm"] == "mock" and h["telephony"] == "mock"
        r = tcl.post("/api/chat", json={"message": "which doctors do you have?"}, headers=AUTH).json()
        assert "Ananya Rao" in r["reply"]
        r = tcl.post("/api/chat", json={"message": f"availability for Pediatrics on {future_date(3)}"}, headers=AUTH).json()
        assert "10:00" in r["reply"]


def test_production_refuses_mock_hms(tmp_path):
    from app.container import build_container
    s = make_settings(tmp_path, hms_api_key="", app_env="production")
    with pytest.raises(RuntimeError):
        build_container(s)


def test_production_refuses_mock_telephony(tmp_path):
    from app.container import build_container
    s = make_settings(tmp_path, app_env="production", telephony_provider="mock")
    with pytest.raises(RuntimeError):
        build_container(s)
