import json

import httpx
import pytest

from app.models.schemas import SessionState
from app.services.openai_service import LLMError
from tests.conftest import patient_sms, FakeLLM, HMS, appt_payload, future_date, say, slots_payload, tc

D = future_date(5)
BOOK_ARGS = {"doctor_id": "DOC-001", "date": D, "start_time": "10:00", "patient_name": "Asha Verma",
             "patient_phone": "+91 98765 43210"}


def new_session(c, caller="+919876543210"):
    return c.calls.create("chat", caller_number=caller).id


def tool_results(c, sid):
    return [json.loads(m["content"]) for m in c.calls.load_state(sid).messages if m["role"] == "tool"]


async def test_faq_answer_from_approved_file(make_container):
    c = make_container(FakeLLM([tc("lookup_faq", {"question": "what are your opening hours"}), say("We are open 9 to 6 (sample).")]))
    sid = new_session(c)
    turn = await c.receptionist.handle_turn(sid, "what are your opening hours?")
    res = tool_results(c, sid)[0]
    assert res["found"] and "SAMPLE" in res["answers"][0]["answer"] and res["answers"][0]["verified"] is False
    assert "open" in turn.reply


async def test_faq_unknown_topic_is_not_invented(make_container):
    c = make_container()
    r = c.receptionist.tools.faq.lookup("do you have a helipad zzz")
    assert r["found"] is False


async def test_list_doctors_tool_calls_hms(make_container, mock_hms):
    mock_hms.get(f"{HMS}/hms/doctors").mock(return_value=httpx.Response(200, json={"data": {"doctors": [
        {"id": "DOC-001", "name": "Dr. Ananya Rao", "specialty": "Pediatrics"}]}}))
    c = make_container(FakeLLM([tc("list_doctors", {}), say("Dr. Ananya Rao in Pediatrics.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "which doctors do you have")
    assert tool_results(c, sid)[0]["doctors"][0]["name"] == "Dr. Ananya Rao"


async def test_booking_requires_explicit_confirmation(make_container, mock_hms, sms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D)))
    post = mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=D)))
    c = make_container(FakeLLM([
        tc("prepare_booking", BOOK_ARGS), say("Please confirm: Dr. Ananya Rao on the date at 10:00. Say yes."),  # turn 1
        tc("book_appointment", BOOK_ARGS, "c2"), say("Booked, ID APT-261019-5YUQEO."),                           # turn 2 (yes)
    ]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "Book Dr Rao at 10 for Asha Verma 9876543210")
    assert post.call_count == 0  # nothing booked before confirmation
    await c.receptionist.handle_turn(sid, "yes, please confirm")
    assert post.call_count == 1 and len(patient_sms(sms)) == 1
    assert c.calls.load_state(sid).facts.appointment_ref == "APT-261019-5YUQEO"


async def test_model_cannot_book_in_same_turn_as_prepare(make_container, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D)))
    post = mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=D)))
    c = make_container(FakeLLM([tc("prepare_booking", BOOK_ARGS), tc("book_appointment", BOOK_ARGS, "c2"),
                                say("Please confirm with a yes.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "yes book it")  # even an early 'yes' cannot skip the read-back turn
    assert post.call_count == 0
    assert tool_results(c, sid)[1]["status"] == "confirmation_required"


async def test_non_affirmative_reply_does_not_book(make_container, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D)))
    post = mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=D)))
    c = make_container(FakeLLM([tc("prepare_booking", BOOK_ARGS), say("Confirm?"),
                                tc("book_appointment", BOOK_ARGS, "c2"), say("Okay, not booking.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "book it")
    await c.receptionist.handle_turn(sid, "no, wait, change the time")
    assert post.call_count == 0


async def test_unavailable_slot_not_staged(make_container, mock_hms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D, slots=("10:30",))))
    c = make_container(FakeLLM([tc("prepare_booking", BOOK_ARGS), say("10:00 is taken; 10:30 is free.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "book 10am")
    assert tool_results(c, sid)[0]["status"] == "slot_not_available"
    assert c.calls.load_state(sid).pending_booking is None


async def test_booking_timeout_triggers_handoff_and_alert(make_container, mock_hms, email, sms):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D)))
    mock_hms.post(f"{HMS}/hms/appointments").mock(side_effect=httpx.ReadTimeout("slow"))
    c = make_container(FakeLLM([tc("prepare_booking", BOOK_ARGS), say("Confirm?"),
                                tc("book_appointment", BOOK_ARGS, "c2"), say("I could not confirm; staff will call.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "book")
    await c.receptionist.handle_turn(sid, "yes")
    st = c.calls.load_state(sid)
    assert st.facts.outcome == "booking_uncertain" and st.facts.callback_required
    assert len(c.callbacks.for_call(sid)) == 1 and patient_sms(sms) == []
    assert any("Callback" in m["subject"] for m in email.sent)


async def _seed_booking(c, mock_hms):
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=D)))
    await c.appointments.book(doctor_id="DOC-001", date=D, start_time="10:00", patient_name="Asha Verma",
                              patient_phone="+919876543210")


CANCEL = {"appointment_id": "APT-261019-5YUQEO", "verification_context": {
    "patient_name": "Asha Verma", "patient_phone": "9876543210", "caller_confirms_cancellation": True}}


async def test_cancellation_verified_then_confirmed(make_container, mock_hms):
    c = make_container(FakeLLM([tc("cancel_appointment", CANCEL), say("Cancel Dr Rao 10:00? say yes"),
                                tc("cancel_appointment", CANCEL, "c2"), say("Cancelled.")]))
    await _seed_booking(c, mock_hms)
    patch = mock_hms.patch(f"{HMS}/hms/appointments/APT-261019-5YUQEO").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"id": "APT-261019-5YUQEO", "status": "cancelled"}}))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "cancel my appointment APT-261019-5YUQEO")
    assert patch.call_count == 0
    await c.receptionist.handle_turn(sid, "yes cancel it")
    assert patch.call_count == 1
    assert c.calls.load_state(sid).facts.outcome == "appointment_cancelled"


async def test_cancellation_unverified_escalates_to_staff(make_container, mock_hms):
    c = make_container(FakeLLM([tc("cancel_appointment", {**CANCEL, "verification_context": {
        "patient_name": "Someone Else", "patient_phone": "9000000000", "caller_confirms_cancellation": True}}),
        say("Staff will verify and call you back.")]))
    await _seed_booking(c, mock_hms)
    patch = mock_hms.patch(f"{HMS}/hms/appointments/APT-261019-5YUQEO")
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "yes cancel APT-261019-5YUQEO")
    assert patch.call_count == 0
    assert tool_results(c, sid)[0]["status"] == "needs_staff_verification"
    assert c.calls.load_state(sid).facts.outcome == "cancellation_escalated"


async def test_human_request_triggers_handoff_without_llm(make_container, email):
    llm = FakeLLM([])
    c = make_container(llm)
    sid = new_session(c)
    turn = await c.receptionist.handle_turn(sid, "I want to talk to a human please")
    assert turn.handoff_requested and llm.calls == 0
    assert len(c.callbacks.for_call(sid)) == 1 and email.sent


async def test_emergency_is_redirected_and_not_triaged(make_container):
    llm = FakeLLM([])
    c = make_container(llm)
    turn = await c.receptionist.handle_turn(new_session(c), "I have chest pain what should I take")
    assert "112" in turn.reply and llm.calls == 0


async def test_malformed_tool_arguments_handled(make_container):
    c = make_container(FakeLLM([tc("check_availability", "{not json"), tc("check_availability", {"date": "tomorrow"}, "c2"),
                                say("Which date and specialty?")]))
    sid = new_session(c)
    turn = await c.receptionist.handle_turn(sid, "availability please")
    r = tool_results(c, sid)
    assert r[0]["error"] == "malformed_arguments" and r[1]["error"] == "invalid_arguments"
    assert turn.reply == "Which date and specialty?"


async def test_tool_call_limit_per_turn(make_container):
    loop = [tc("lookup_faq", {"question": "hours"}, f"c{i}") for i in range(20)]
    c = make_container(FakeLLM(loop))
    sid = new_session(c)
    turn = await c.receptionist.handle_turn(sid, "hours?")
    state = c.calls.load_state(sid)
    assert sum(1 for m in state.messages if m['role'] == 'tool') == 5  # MAX_TOOL_CALLS_PER_TURN
    assert turn.reply  # loop terminated with a fallback reply


async def test_llm_failure_falls_back_to_handoff(make_container):
    c = make_container(FakeLLM([LLMError("boom")]))
    sid = new_session(c)
    turn = await c.receptionist.handle_turn(sid, "hello")
    assert turn.end_call and turn.handoff_requested and len(c.callbacks.for_call(sid)) == 1


async def test_repeated_silence_leads_to_handoff(make_container):
    c = make_container()
    sid = new_session(c)
    r1 = await c.receptionist.handle_silence(sid)
    r2 = await c.receptionist.handle_silence(sid)
    r3 = await c.receptionist.handle_silence(sid)
    assert not r1.end_call and not r2.end_call and r3.end_call and r3.handoff_requested


async def test_call_summary_contents_and_no_phone_numbers(make_container, mock_hms, email):
    mock_hms.get(f"{HMS}/hms/availability").mock(return_value=httpx.Response(200, json=slots_payload(D)))
    mock_hms.post(f"{HMS}/hms/appointments").mock(return_value=httpx.Response(201, json=appt_payload(date=D)))
    c = make_container(FakeLLM([tc("prepare_booking", BOOK_ARGS), say("Confirm?"), tc("book_appointment", BOOK_ARGS, "c2"),
                                say("Booked."), tc("generate_call_summary", {"intent": "book", "outcome": "ok",
                                "notes": "caller 9876543210 booked", "call_finished": True}, "c3"), say("Goodbye.")]))
    sid = new_session(c)
    await c.receptionist.handle_turn(sid, "book")
    await c.receptionist.handle_turn(sid, "yes")
    turn = await c.receptionist.handle_turn(sid, "no thanks, bye")
    assert turn.end_call
    s = await c.summaries.finalize(sid)
    assert s.caller_intent == "book_appointment" and s.outcome == "appointment_confirmed"
    assert s.appointment_reference == "APT-261019-5YUQEO" and s.callback_required is False
    assert "9876543210" not in s.summary
    assert c.calls.load_state(sid).messages == []  # transcript purged
    n = len([m for m in email.sent if "summary" in m["subject"].lower()])
    await c.summaries.finalize(sid)  # idempotent: no second summary email
    assert len([m for m in email.sent if "summary" in m["subject"].lower()]) == n == 1
