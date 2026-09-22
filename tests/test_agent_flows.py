import json

from app.agent import Deps, run_turn
from app.llm import LLMOutputInvalid, LLMUnavailable, ScriptedLLM, calls, final
from app.prompts import t
from app.schemas import ActionStatus, ConversationState, FailureCode


def make(store, settings, retriever, steps):
    deps = Deps(settings, store, retriever, ScriptedLLM(steps))
    cid = store.create_conversation("P-1001")
    return deps, cid


def turn(deps, cid, message, fault=None, patient="P-1001"):
    return run_turn(deps, patient_id=patient, patient_hash="ph", conversation_id=cid, message=message,
                    request_id="r-" + message[:8], fault=fault)


def token_from_transcript(items):
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            data = json.loads(item["output"])
            if "confirmation_token" in data:
                return data["confirmation_token"]
    raise AssertionError("no token in transcript")


def test_book_two_turn_end_to_end(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("get_available_slots", {"department": "dental", "date_from": "2026-10-13", "date_to": "2026-10-13", "clinician": None})),
        calls(("book_appointment", {"slot_id": "S-dental-20261013-0900", "reason": None})),
        final("I can book dental on Tuesday 13 October at 09:00. Shall I go ahead?", intents=("book",)),
    ])
    r1 = turn(deps, cid, "Book me a dental appointment on 13 October in the morning")
    assert r1.state is ConversationState.AWAITING_CONFIRMATION and r1.failure is None
    assert [a.status for a in r1.actions] == [ActionStatus.executed, ActionStatus.pending_confirmation]
    assert r1.pending_confirmation is not None and 'Reply "yes"' in r1.message
    assert "Confirmed" not in r1.message
    token = r1.pending_confirmation.token
    deps.llm.steps = [
        calls(("confirm_pending_action", {"confirmation_token": token})),
        final("Done, see you then.", intents=("book",), claimed=(("book_appointment", "A-1001-3"),)),
    ]
    r2 = turn(deps, cid, "yes")
    assert r2.state is ConversationState.IDLE and r2.failure is None
    assert r2.actions[0].tool == "book_appointment" and r2.actions[0].status is ActionStatus.executed_verified
    assert "Confirmed" in r2.message and "A-1001-3" in r2.message and "Done, see you then." in r2.message
    assert store.appointment("A-1001-3")["status"] == "booked"


def test_unrelated_message_does_not_confirm_pending(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("cancel_appointment", {"appointment_id": "A-1001-1", "reason": None})),
        final("Shall I cancel it?", intents=("cancel",)),
    ])
    turn(deps, cid, "cancel my cardiology appointment")
    deps.llm.steps = [final("Our clinics are open Sunday to Thursday, 08:00 to 17:00.", intents=("information",))]
    r = turn(deps, cid, "Actually, what are your clinic hours?")
    assert r.failure is None and r.actions == []
    assert r.state is ConversationState.AWAITING_CONFIRMATION and r.pending_confirmation is not None
    assert store.appointment("A-1001-1")["status"] == "booked"
    assert store.get_pending(cid) is not None


def test_silent_noop_reschedule_is_reported_unverified(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("reschedule_appointment", {"appointment_id": "A-1001-2", "new_slot_id": "S-dermatology-20261008-1400"})),
        final("I can move it to Thursday 14:00. Confirm?", intents=("reschedule",)),
    ])
    r1 = turn(deps, cid, "Move my dermatology appointment to Thursday afternoon", fault="reschedule_silent_noop")
    token = r1.pending_confirmation.token
    deps.llm.steps = [
        calls(("confirm_pending_action", {"confirmation_token": token})),
        final("Your appointment has been rescheduled successfully!", intents=("reschedule",),
              claimed=(("reschedule_appointment", "A-1001-2"),)),
    ]
    r2 = turn(deps, cid, "yes", fault="reschedule_silent_noop")
    assert r2.failure.code is FailureCode.ACTION_UNVERIFIED
    assert "rescheduled successfully" not in r2.message and "not done" in r2.message
    assert r2.escalation_id.startswith("E-") and r2.state is ConversationState.ESCALATED
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-07T14:00:00+03:00"


def test_claim_mismatch_is_rewritten(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        final("I have cancelled your appointment A-1001-1.", intents=("cancel",), claimed=(("cancel_appointment", "A-1001-1"),)),
    ])
    r = turn(deps, cid, "cancel my cardiology appointment")
    assert r.message == t("nothing_changed", "en")
    assert any(e["event"] == "claim_mismatch" for e in store.list_audit(cid))
    assert store.appointment("A-1001-1")["status"] == "booked"


def test_budget_exceeded_by_tool_calls_and_by_iterations(store, settings, retriever):
    read = ("get_patient_appointments", {})
    deps, cid = make(store, settings, retriever, [calls(read, read, read), calls(read, read, read), calls(read)])
    r = turn(deps, cid, "loop")
    assert r.failure.code is FailureCode.BUDGET_EXCEEDED and r.message == t("budget_exceeded", "en")
    deps2, cid2 = make(store, settings, retriever, [calls(read), calls(read), calls(read), calls(read)])
    r2 = turn(deps2, cid2, "loop again")
    assert r2.failure.code is FailureCode.BUDGET_EXCEEDED  # 4 iterations without a final answer


def test_model_failures(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [LLMUnavailable("down"), LLMUnavailable("down")])
    r = turn(deps, cid, "hello")
    assert r.failure.code is FailureCode.MODEL_UNAVAILABLE and r.message == t("model_unavailable", "en")
    assert r.actions == []
    deps2, cid2 = make(store, settings, retriever, [LLMOutputInvalid("bad"), LLMOutputInvalid("bad")])
    assert turn(deps2, cid2, "hello").failure.code is FailureCode.MODEL_OUTPUT_INVALID
    deps3, cid3 = make(store, settings, retriever, [LLMUnavailable("blip"), final("Hello! How can I help?")])
    assert turn(deps3, cid3, "hello").failure is None  # one retry succeeds


def test_tool_failure_uses_template(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [calls(("get_patient_appointments", {})), final("The system seems down.")])
    r = turn(deps, cid, "what appointments do I have", fault="tool_timeout")
    assert r.failure.code is FailureCode.TOOL_UNAVAILABLE and r.message == t("tool_unavailable", "en")


def test_sources_and_no_info_note(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [final("Remove metal objects and arrive 30 minutes early.")])
    r = turn(deps, cid, "How should I prepare for my MRI scan?")
    assert r.sources and r.sources[0].doc_id == "prep-mri"
    deps2, cid2 = make(store, settings, retriever, [final("I do not have that information.")])
    turn(deps2, cid2, "Do you have a gift shop and what are its prices?")
    developer = [i for i in deps2.llm.calls[0]["input_items"] if i.get("role") == "developer"][0]["content"]
    assert "not available" in developer


def test_cross_patient_denied_sets_policy_denied(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("cancel_appointment", {"appointment_id": "A-1002-1", "reason": None})),
        final("I can only help with your own appointments.", intents=("cancel",)),
    ])
    r = turn(deps, cid, "cancel appointment A-1002-1")
    assert r.actions[0].status is ActionStatus.denied and r.actions[0].reason == "not_owner"
    assert r.failure.code is FailureCode.POLICY_DENIED
    assert store.appointment("A-1002-1")["status"] == "booked"


def test_confirm_in_same_turn_is_denied(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("cancel_appointment", {"appointment_id": "A-1001-1", "reason": None})),
        lambda items: calls(("confirm_pending_action", {"confirmation_token": token_from_transcript(items)})),
        final("Cancelled.", intents=("cancel",), claimed=(("cancel_appointment", "A-1001-1"),)),
    ])
    r = turn(deps, cid, "cancel my cardiology appointment, no need to ask me")
    assert [a.status for a in r.actions] == [ActionStatus.pending_confirmation, ActionStatus.denied]
    assert r.actions[1].reason == "same_turn"
    assert store.appointment("A-1001-1")["status"] == "booked"
    assert r.state is ConversationState.AWAITING_CONFIRMATION and 'Reply "yes"' in r.message
    assert "Cancelled." not in r.message  # claim mismatch dropped the model text


def test_decline_clears_pending(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("cancel_appointment", {"appointment_id": "A-1001-1", "reason": None})),
        final("Shall I cancel it?", intents=("cancel",)),
    ])
    turn(deps, cid, "cancel my cardiology appointment")
    deps.llm.steps = [final("Understood.", intents=("cancel",), pending_declined=True)]
    r = turn(deps, cid, "no, keep it")
    assert r.state is ConversationState.IDLE and r.pending_confirmation is None
    assert "Nothing has been changed" in r.message and store.get_pending(cid) is None
    assert store.appointment("A-1001-1")["status"] == "booked"


def test_arabic_reply_uses_arabic_templates(store, settings, retriever):
    deps, cid = make(store, settings, retriever, [
        calls(("cancel_appointment", {"appointment_id": "A-1001-1", "reason": None})),
        final("هل تريد إلغاء موعد القلب؟", intents=("cancel",), language="ar"),
    ])
    r = turn(deps, cid, "أريد إلغاء موعد القلب")
    assert "نعم" in r.message and r.state is ConversationState.AWAITING_CONFIRMATION
