"""Small shared helpers: timezone, phone handling, masking."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def utcnow() -> datetime:
    """Naive UTC datetime (what we store in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_ist() -> date:
    return datetime.now(IST).date()


def now_ist() -> datetime:
    return datetime.now(IST)


def appointment_start_ist(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def valid_date(value: str) -> bool:
    if not DATE_RE.match(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def valid_time(value: str) -> bool:
    if not TIME_RE.match(value or ""):
        return False
    h, m = value.split(":")
    return 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def normalize_phone(raw: str, default_country_code: str = "91") -> str:
    """Return E.164 (+<digits>) or raise ValueError."""
    if not raw:
        raise ValueError("phone number is empty")
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if plus:
        pass
    elif len(digits) == 10:
        digits = default_country_code + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = default_country_code + digits[1:]
    elif len(digits) == 12 and digits.startswith(default_country_code):
        pass
    else:
        raise ValueError("phone number must be a 10-digit Indian number or include a country code")
    if not 8 <= len(digits) <= 15:
        raise ValueError("phone number length is invalid")
    return "+" + digits


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return "*" * max(len(digits) - 2, 0) + digits[-2:]


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    user, _, domain = email.partition("@")
    return (user[:1] + "***@" + domain)


def last4(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:]
