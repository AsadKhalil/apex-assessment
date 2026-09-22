import pytest

from app.policy import ToolCall, TurnContext, authorize, execute
from app.schemas import ActionStatus, ConversationState, FailureCode
from app.tools import HospitalTools
from tests.test_policy import ctx_for


def run(ctx, settings, store, tools, name, arguments):
    call = ToolCall(f"c{ctx.tool_calls_used + 1}", name, arguments)
    return execute(ctx, settings, store, tools, call, authorize(ctx, settings, store, call))


@pytest.fixture
def tools(store, settings):
    return HospitalTools(store, settings)


def test_read_executes(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "get_patient_appointments", {})
    assert out.status is ActionStatus.executed and len(out.output["appointments"]) == 2
    assert ctx.tool_calls_used == 1
    assert store.list_audit(ctx.conversation_id)[0]["detail"]["tool"] == "get_patient_appointments"


def test_denied_outcome(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "cancel_appointment", {"appointment_id": "A-1002-1"})
    assert out.status is ActionStatus.denied and out.reason == "not_owner"
    assert out.output == {"error": "denied", "reason": "not_owner"}
    assert store.appointment("A-1002-1")["status"] == "booked"


def test_write_creates_pending_and_changes_state(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "reschedule_appointment",
              {"appointment_id": "A-1001-2", "new_slot_id": "S-dermatology-20261008-1400"})
    assert out.status is ActionStatus.pending_confirmation
    pending = store.get_pending(ctx.conversation_id)
    assert pending["token"] == out.output["confirmation_token"]
    assert "Thursday 08 October 2026 at 14:00" in out.summary
    assert ctx.state is ConversationState.AWAITING_CONFIRMATION
    assert store.get_conversation(ctx.conversation_id)["state"] == "AWAITING_CONFIRMATION"
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-07T14:00:00+03:00"  # nothing changed yet


def test_pending_denied_when_department_limit(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "book_appointment",
              {"slot_id": "S-cardiology-20261013-0900", "reason": None})
    assert out.status is ActionStatus.denied and out.reason.startswith("department_limit")
    assert store.get_pending(ctx.conversation_id) is None
    assert store.appointment("A-1001-3") is None


def test_second_proposal_replaces_pending(store, settings, tools):
    ctx = ctx_for(store)
    first = run(ctx, settings, store, tools, "cancel_appointment", {"appointment_id": "A-1001-1"})
    second = run(ctx, settings, store, tools, "cancel_appointment", {"appointment_id": "A-1001-2"})
    assert store.get_pending(ctx.conversation_id)["token"] == second.output["confirmation_token"]
    assert first.output["confirmation_token"] != second.output["confirmation_token"]


def test_pending_denied_when_slot_taken(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "book_appointment", {"slot_id": "S-cardiology-20261008-0900"})
    assert out.status is ActionStatus.denied and out.reason.startswith("slot_unavailable")
    assert store.get_pending(ctx.conversation_id) is None


def test_confirm_executes_verifies_and_is_idempotent(store, settings, tools):
    ctx1 = ctx_for(store, turn=1)
    proposal = run(ctx1, settings, store, tools, "reschedule_appointment",
                   {"appointment_id": "A-1001-2", "new_slot_id": "S-dermatology-20261008-1400"})
    token = proposal.output["confirmation_token"]
    cid = ctx1.conversation_id
    store.next_turn(cid)
    ctx2 = TurnContext(request_id="r2", conversation_id=cid, patient_id="P-1001", patient_hash="ph",
                       turn_index=2, state=ConversationState.AWAITING_CONFIRMATION, now=store.now())
    out = run(ctx2, settings, store, tools, "confirm_pending_action", {"confirmation_token": token})
    assert out.status is ActionStatus.executed_verified and out.verified is True
    assert out.executed_tool == "reschedule_appointment" and out.appointment_id == "A-1001-2"
    assert out.output["verified"] is True and out.output["new_start_time"] == "2026-10-08T14:00:00+03:00"
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-08T14:00:00+03:00"
    assert store.ledger_get(out.idempotency_key)["verified"] is True
    assert store.get_pending(cid) is None and ctx2.state is ConversationState.IDLE
    # replay: the same pending action re-presented (duplicate delivery) must not execute twice
    store.conn.execute("UPDATE pending_actions SET status='pending' WHERE token=?", (token,))
    ctx3 = TurnContext(request_id="r3", conversation_id=cid, patient_id="P-1001", patient_hash="ph",
                       turn_index=3, state=ConversationState.AWAITING_CONFIRMATION, now=store.now())
    again = run(ctx3, settings, store, tools, "confirm_pending_action", {"confirmation_token": token})
    assert again.status is ActionStatus.executed_verified and again.idempotency_key == out.idempotency_key
    assert store.slot("S-dermatology-20261007-1400")["status"] == "open"  # not re-swapped


def test_silent_noop_is_unverified_and_escalates(store, settings):
    faulty = HospitalTools(store, settings, fault="reschedule_silent_noop")
    ctx1 = ctx_for(store, turn=1)
    proposal = run(ctx1, settings, store, faulty, "reschedule_appointment",
                   {"appointment_id": "A-1001-2", "new_slot_id": "S-dermatology-20261008-1400"})
    ctx2 = TurnContext(request_id="r2", conversation_id=ctx1.conversation_id, patient_id="P-1001", patient_hash="ph",
                       turn_index=2, state=ConversationState.AWAITING_CONFIRMATION, now=store.now())
    out = run(ctx2, settings, store, faulty, "confirm_pending_action",
              {"confirmation_token": proposal.output["confirmation_token"]})
    assert out.status is ActionStatus.failed and out.failure is FailureCode.ACTION_UNVERIFIED
    assert out.verified is False and out.escalation_id.startswith("E-")
    assert out.output["verified"] is False
    assert ctx2.state is ConversationState.ESCALATED
    assert store.ledger_get(out.idempotency_key)["verified"] is False
    assert store.appointment("A-1001-2")["start_time"] == "2026-10-07T14:00:00+03:00"


def test_tool_failures_map_to_codes(store, settings):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, HospitalTools(store, settings, fault="malformed_result"), "get_patient_appointments", {})
    assert out.status is ActionStatus.failed and out.failure is FailureCode.TOOL_MALFORMED
    out2 = run(ctx, settings, store, HospitalTools(store, settings, fault="tool_timeout"), "get_patient_appointments", {})
    assert out2.failure is FailureCode.TOOL_UNAVAILABLE
    assert "instruction" in out2.output


def test_escalation_executes_directly(store, settings, tools):
    ctx = ctx_for(store)
    out = run(ctx, settings, store, tools, "create_human_escalation",
              {"reason_category": "user_request", "summary": "wants a human"})
    assert out.status is ActionStatus.executed and out.escalation_id.startswith("E-")
    assert ctx.state is ConversationState.ESCALATED
    assert store.get_conversation(ctx.conversation_id)["state"] == "ESCALATED"
