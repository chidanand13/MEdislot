from datetime import timedelta

import httpx

from app.container import build_container
from app.utils import appointment_start_ist, to_utc_naive, utcnow
from tests.conftest import patient_sms, FakeLLM, future_date, make_settings

KW = dict(appointment_id="APT-R1", patient_name="Asha Verma", patient_phone="+919876543210",
          doctor_name="Dr. Ananya Rao", start_time="10:00")


async def test_reminder_scheduled_24h_before_in_ist(make_container):
    c = make_container()
    d = future_date(5)
    row = c.reminders.schedule(date=d, **KW)
    expected = to_utc_naive(appointment_start_ist(d, "10:00") - timedelta(hours=24))
    assert row.remind_at == expected and row.status == "pending"


async def test_duplicate_reminder_prevented(make_container):
    c = make_container()
    d = future_date(5)
    c.reminders.schedule(date=d, **KW)
    c.reminders.schedule(date=d, **KW)
    assert len(c.reminders.list_for("APT-R1")) == 1


async def test_due_reminder_sent_once(make_container, sms):
    c = make_container()
    d = future_date(5)
    c.reminders.schedule(date=d, **KW)
    future = utcnow() + timedelta(days=10)
    assert await c.reminders.run_due(now=utcnow()) == 0  # not due yet
    assert await c.reminders.run_due(now=to_utc_naive(appointment_start_ist(d, "10:00") - timedelta(hours=23))) == 1
    assert await c.reminders.run_due(now=to_utc_naive(appointment_start_ist(d, "10:00") - timedelta(hours=22))) == 0
    assert len(patient_sms(sms)) == 1 and "reminder" in sms.sent[0]["body"].lower()
    assert c.reminders.list_for("APT-R1")[0].status == "sent"


async def test_cancelled_appointment_reminder_not_sent(make_container, sms):
    c = make_container()
    d = future_date(5)
    c.reminders.schedule(date=d, **KW)
    assert c.reminders.cancel("APT-R1") == 1
    assert await c.reminders.run_due(now=to_utc_naive(appointment_start_ist(d, "10:00"))) == 0
    assert patient_sms(sms) == [] and c.reminders.list_for("APT-R1")[0].status == "cancelled"


async def test_reminder_survives_restart(tmp_path, sms, email):
    s = make_settings(tmp_path)
    d = future_date(5)
    c1 = build_container(s, llm=FakeLLM([]), email=email, sms=sms)
    c1.reminders.schedule(date=d, **KW)
    c1.engine.dispose()
    c2 = build_container(s, llm=FakeLLM([]), email=email, sms=sms)  # "restart": new objects, same database file
    assert len(c2.reminders.list_for("APT-R1")) == 1
    sent = await c2.reminders.run_due(now=to_utc_naive(appointment_start_ist(d, "10:00") - timedelta(hours=23)))
    assert sent == 1 and len(patient_sms(sms)) == 1
    c2.engine.dispose()


async def test_reminder_skipped_when_booked_too_late(make_container):
    c = make_container()
    row = c.reminders.schedule(date=future_date(0), **{**KW, "start_time": "23:59"})
    assert row.status == "skipped"


async def test_interrupted_sending_state_recovered(make_container):
    c = make_container()
    d = future_date(5)
    row = c.reminders.schedule(date=d, **KW)
    assert c.reminders.repo.claim(row.id)  # simulate crash after claim
    assert c.reminders.repo.recover_stuck() == 1
    assert c.reminders.list_for("APT-R1")[0].status == "pending"
