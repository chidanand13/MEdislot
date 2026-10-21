"""SQLAlchemy tables. Times are stored as naive UTC."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils import utcnow


class Base(DeclarativeBase):
    pass


class CallRow(Base):
    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    provider_call_sid: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    channel: Mapped[str] = mapped_column(String(10), default="voice")  # voice | chat
    caller_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | completed
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BookingOperationRow(Base):
    __tablename__ = "booking_operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # also the Idempotency-Key
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    call_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    doctor_id: Mapped[str] = mapped_column(String(40))
    date: Mapped[str] = mapped_column(String(10))
    start_time: Mapped[str] = mapped_column(String(5))
    patient_name: Mapped[str] = mapped_column(String(100))
    patient_phone: Mapped[str] = mapped_column(String(20))
    # in_flight | confirmed | pending | conflict | failed | uncertain | cancelled
    status: Mapped[str] = mapped_column(String(20), default="in_flight")
    appointment_id: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    doctor_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReminderRow(Base):
    __tablename__ = "reminders"
    __table_args__ = (UniqueConstraint("appointment_id", "offset_minutes", name="uq_reminder"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appointment_id: Mapped[str] = mapped_column(String(60), index=True)
    offset_minutes: Mapped[int] = mapped_column(Integer)
    remind_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # pending | sending | sent | failed | cancelled | skipped
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    patient_name: Mapped[str] = mapped_column(String(100))
    patient_phone: Mapped[str] = mapped_column(String(20))
    doctor_name: Mapped[str] = mapped_column(String(100))
    date: Mapped[str] = mapped_column(String(10))
    start_time: Mapped[str] = mapped_column(String(5))
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CallbackRow(Base):
    __tablename__ = "callbacks"
    __table_args__ = (UniqueConstraint("call_id", "reason", name="uq_callback_call_reason"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(String(300))
    caller_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    patient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NotificationRow(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(10))  # email | sms
    recipient: Mapped[str] = mapped_column(String(80))  # masked
    status: Mapped[str] = mapped_column(String(10), default="sending")  # sending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
