"""Central configuration, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # general
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./medislot.db"
    hospital_name: str = "Sample City Hospital"
    internal_api_key: SecretStr = SecretStr("")
    public_base_url: str = ""
    rate_limit_per_minute: int = 120
    timezone: str = "Asia/Kolkata"
    faq_path: str = "app/data/hospital_faqs.json"

    # HMS
    hms_base_url: str = "https://akhil12.app.n8n.cloud/webhook"
    hms_api_key: SecretStr = SecretStr("")
    hms_timeout_seconds: float = 10.0
    hms_get_retries: int = 2
    hms_path_prefix: str = "/hms"  # "/hms" for the n8n webhook base; "" if HMS_BASE_URL already ends in /api (Akhil's Express service)
    hms_mock_mode: bool = False  # force the fictional local HMS even if a key exists

    # OpenAI
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 20.0

    # telephony
    telephony_provider: str = "mock"  # mock | twilio
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    stt_language: str = "en-IN"
    tts_voice: str = "Polly.Aditi"
    handoff_dial_number: str = ""

    # notifications
    email_provider: str = "mock"  # mock | smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    sms_provider: str = "mock"  # mock | twilio
    staff_alert_email: str = ""
    staff_alert_phone: str = ""
    call_summary_email: str = ""
    reminder_offset_hours: int = 24
    reminder_poll_seconds: int = 30
    reminders_enabled: bool = True
    reminder_loop_enabled: bool = True

    # receptionist
    max_tool_calls_per_turn: int = 5
    max_misunderstandings: int = 3
    retain_transcripts: bool = False
    emergency_number: str = "112"

    # ---- derived ----
    @property
    def hms_is_mock(self) -> bool:
        return self.hms_mock_mode or not self.hms_api_key.get_secret_value()

    @property
    def summary_email(self) -> str:
        return self.call_summary_email or self.staff_alert_email

    def secret_values(self) -> list[str]:
        vals = [
            self.hms_api_key, self.openai_api_key, self.twilio_auth_token,
            self.smtp_password, self.internal_api_key,
        ]
        return [v.get_secret_value() for v in vals if v.get_secret_value()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
