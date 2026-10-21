"""Chooses providers from settings. Missing credentials => safe MOCK provider (never raises)."""
from __future__ import annotations

import logging

from app.config import Settings
from app.providers.email.base import EmailProvider, MockEmailProvider, SMTPEmailProvider
from app.providers.sms.base import MockSMSProvider, SMSProvider, TwilioSMSProvider
from app.providers.telephony.base import TelephonyProvider
from app.providers.telephony.mock import MockTelephonyProvider
from app.providers.telephony.twilio import TwilioTelephonyProvider

log = logging.getLogger(__name__)


def build_email(s: Settings) -> EmailProvider:
    if s.email_provider == "smtp":
        if s.smtp_host and s.smtp_from:
            return SMTPEmailProvider(s.smtp_host, s.smtp_port, s.smtp_username,
                                     s.smtp_password.get_secret_value(), s.smtp_from)
        log.warning("EMAIL_PROVIDER=smtp but SMTP credentials are missing -> using MOCK email")
    return MockEmailProvider()


def build_sms(s: Settings) -> SMSProvider:
    if s.sms_provider == "twilio":
        if s.twilio_account_sid and s.twilio_auth_token.get_secret_value() and s.twilio_phone_number:
            return TwilioSMSProvider(s.twilio_account_sid, s.twilio_auth_token.get_secret_value(),
                                     s.twilio_phone_number)
        log.warning("SMS_PROVIDER=twilio but Twilio credentials are missing -> using MOCK sms")
    return MockSMSProvider()


def build_telephony(s: Settings) -> TelephonyProvider:
    if s.telephony_provider == "twilio":
        if s.twilio_auth_token.get_secret_value() and s.public_base_url:
            return TwilioTelephonyProvider(s)
        log.warning("TELEPHONY_PROVIDER=twilio but TWILIO_AUTH_TOKEN/PUBLIC_BASE_URL missing -> MOCK telephony")
    return MockTelephonyProvider()
