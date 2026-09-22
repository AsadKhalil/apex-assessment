from datetime import timedelta

from app.policy import Decision, ToolCall, TurnContext, authorize
from app.schemas import ConversationState


def ctx_for(store, patient="P-1001", state=ConversationState.IDLE, turn=1):
    cid = store.create_conversation(patient)
    for _ in range(turn):
        store.next_turn(cid)
    store.set_state(cid, state.value)
    return TurnContext(request_id="r1", conversation_id=cid, patient_id=patient, patient_hash="ph",
                       turn_index=turn, state=state, now=store.now())


def set_pending(store, ctx, token="tok", turn_created=1, expires_in=600, tool="cancel_appointment",
                args=None):
    args = args or {"appointment_id": "A-1001-1"}
    store.set_pending(ctx.conversation_id, token=token, tool=tool, args=args, args_hash="h",
                      summary="Cancel A-1001-1", turn_created=turn_created,
                      expires_at=(ctx.now + timedelta(seconds=expires_in)).isoformat())


def test_unknown_tool_denied(store, settings):
    d = authorize(ctx_for(store), settings, store, ToolCall("c1", "delete_everything", {}))
    assert d == Decision("DENY", "unknown_tool")


def test_budget_denied(store, settings):
    ctx = ctx_for(store)
    ctx.tool_calls_used = settings.max_tool_calls
    d = authorize(ctx, settings, store, ToolCall("c1", "get_patient_appointments", {}))
    assert d.kind == "DENY" and d.reason == "budget"


def test_invalid_args_denied(store, settings):
    d = authorize(ctx_for(store), settings, store, ToolCall("c1", "book_appointment", {"slot_id": "S-1", "patient_id": "P-1002"}))
    assert d.kind == "DENY" and d.reason.startswith("invalid_args")


def test_reads_allowed_and_writes_pending(store, settings):
    ctx = ctx_for(store)
    assert authorize(ctx, settings, store, ToolCall("c1", "get_patient_appointments", {})).kind == "ALLOW"
    d = authorize(ctx, settings, store, ToolCall("c2", "cancel_appointment", {"appointment_id": "A-1001-1"}))
    assert d.kind == "PENDING" and d.args.appointment_id == "A-1001-1"


def test_ownership_denied_without_confirming_existence(store, settings):
    d = authorize(ctx_for(store), settings, store, ToolCall("c1", "cancel_appointment", {"appointment_id": "A-1002-1"}))
    assert d == Decision("DENY", "not_owner", d.args)
    d2 = authorize(ctx_for(store), settings, store, ToolCall("c1", "reschedule_appointment",
                                                              {"appointment_id": "A-9999-9", "new_slot_id": "S-cardiology-20261013-0900"}))
    assert d2.reason == "not_owner"


def test_escalated_state_blocks_writes_and_confirm(store, settings):
    ctx = ctx_for(store, state=ConversationState.ESCALATED, turn=2)
    set_pending(store, ctx, turn_created=1)
    assert authorize(ctx, settings, store, ToolCall("c1", "cancel_appointment", {"appointment_id": "A-1001-1"})).reason == "escalated_no_writes"
    assert authorize(ctx, settings, store, ToolCall("c2", "confirm_pending_action", {"confirmation_token": "tok"})).reason == "escalated_no_writes"
    assert authorize(ctx, settings, store, ToolCall("c3", "get_patient_appointments", {})).kind == "ALLOW"


def test_confirm_requires_pending_and_matching_token(store, settings):
    ctx = ctx_for(store, turn=2)
    assert authorize(ctx, settings, store, ToolCall("c1", "confirm_pending_action", {"confirmation_token": "tok"})).reason == "no_pending"
    set_pending(store, ctx, turn_created=1)
    assert authorize(ctx, settings, store, ToolCall("c1", "confirm_pending_action", {"confirmation_token": "other"})).reason == "token_mismatch"
    assert authorize(ctx, settings, store, ToolCall("c1", "confirm_pending_action", {"confirmation_token": "tok"})).kind == "ALLOW"


def test_confirm_never_in_same_turn(store, settings):
    ctx = ctx_for(store, turn=1)
    set_pending(store, ctx, turn_created=1)
    assert authorize(ctx, settings, store, ToolCall("c1", "confirm_pending_action", {"confirmation_token": "tok"})).reason == "same_turn"


def test_confirm_expired(store, settings):
    ctx = ctx_for(store, turn=2)
    set_pending(store, ctx, turn_created=1, expires_in=-1)
    assert authorize(ctx, settings, store, ToolCall("c1", "confirm_pending_action", {"confirmation_token": "tok"})).reason == "expired"
