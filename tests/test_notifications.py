import httpx

from app.config import Settings
from app.providers.email.base import MockEmailProvider, SMTPEmailProvider
from app.providers.factory import build_email, build_sms, build_telephony
from app.providers.sms.base import MockSMSProvider, TwilioSMSProvider
from app.providers.telephony.mock import MockTelephonyProvider


async def test_confirmation_sms_content_and_duplicate_prevention(make_container, sms):
    c = make_container()
    kw = dict(appointment_id="APT-1", patient_name="Asha Verma", patient_phone="+919876543210",
              doctor_name="Dr. Ananya Rao", date="2026-10-30", start_time="10:00")
    r1 = await c.notifications.send_booking_confirmation(**kw)
    r2 = await c.notifications.send_booking_confirmation(**kw)
    assert r1.sent and r2.duplicate and not r2.sent
    assert len(sms.sent) == 1
    body = sms.sent[0]["body"]
    for part in ("Asha Verma", "Dr. Ananya Rao", "2026-10-30", "10:00", "APT-1"):
        assert part in body


async def test_failed_delivery_is_tracked_and_can_retry(make_container):
    class Flaky(MockSMSProvider):
        n = 0
        async def send(self, to, body):
            Flaky.n += 1
            from app.providers.email.base import ProviderResult
            return ProviderResult(ok=Flaky.n > 1, error=None if Flaky.n > 1 else "boom")
    c = make_container(sms=Flaky())
    kw = dict(appointment_id="APT-2", patient_name="Asha", patient_phone="+919876543210",
              doctor_name="Dr X", date="2026-10-30", start_time="10:00")
    assert (await c.notifications.send_booking_confirmation(**kw)).sent is False
    assert c.notifications.repo.get("confirm:APT-2").status == "failed"
    assert (await c.notifications.send_booking_confirmation(**kw)).sent is True
    assert c.notifications.repo.get("confirm:APT-2").status == "sent"


async def test_staff_alert_goes_to_email_and_sms(make_container, email, sms):
    c = make_container()
    await c.notifications.alert_staff("callback", "Callback required", "details", "cb:1")
    await c.notifications.alert_staff("callback", "Callback required", "details", "cb:1")  # duplicate
    assert len(email.sent) == 1 and len(sms.sent) == 1


async def test_notification_log_masks_recipient(make_container):
    c = make_container()
    await c.notifications.send_booking_confirmation(appointment_id="APT-3", patient_name="A B", patient_phone="+919876543210",
                                                    doctor_name="D", date="2026-10-30", start_time="10:00")
    row = c.notifications.repo.get("confirm:APT-3")
    assert "9876543210" not in row.recipient and row.recipient.endswith("10")


def test_missing_credentials_fall_back_to_mock_providers():
    s = Settings(_env_file=None, email_provider="smtp", sms_provider="twilio", telephony_provider="twilio")
    assert isinstance(build_email(s), MockEmailProvider)
    assert isinstance(build_sms(s), MockSMSProvider)
    assert isinstance(build_telephony(s), MockTelephonyProvider)


def test_live_providers_selected_when_credentials_present():
    s = Settings(_env_file=None, email_provider="smtp", smtp_host="smtp.example.invalid", smtp_from="a@example.invalid",
                 sms_provider="twilio", twilio_account_sid="ACxxx", twilio_auth_token="tok", twilio_phone_number="+10000000000")
    assert isinstance(build_email(s), SMTPEmailProvider) and isinstance(build_sms(s), TwilioSMSProvider)


async def test_twilio_sms_provider_uses_mocked_http():
    def handler(req: httpx.Request):
        assert "Messages.json" in str(req.url)
        return httpx.Response(201, json={"sid": "SM123"})
    p = TwilioSMSProvider("ACxxx", "tok", "+10000000000", transport=httpx.MockTransport(handler))
    assert (await p.send("+919876543210", "hi")).message_id == "SM123"
