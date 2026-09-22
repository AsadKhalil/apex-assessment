"""The gate. Every tool call the model proposes passes through authorize() and then execute()."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas import (
    ActionStatus,
    ConversationState,
    CreateHumanEscalationArgs,
    FailureCode,
    GetPatientAppointmentsArgs,
    ReasonCategory,
)
from app.state import Store
from app.tools import (
    TOOL_SPECS,
    AppointmentNotActive,
    DepartmentLimit,
    HospitalTools,
    NotOwner,
    SlotUnavailable,
    ToolError,
    ToolMalformed,
)


@dataclass
class TurnContext:
    request_id: str
    conversation_id: str
    patient_id: str
    patient_hash: str
    turn_index: int
    state: ConversationState
    now: datetime
    tool_calls_used: int = 0
    iterations_used: int = 0


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Decision:
    kind: Literal["ALLOW", "DENY", "PENDING"]
    reason: str | None = None
    args: BaseModel | None = None


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorize(ctx: TurnContext, settings: Settings, store: Store, call: ToolCall) -> Decision:
    """Order matters and is documented in the spec (section 9). First hit wins."""
    spec = TOOL_SPECS.get(call.name)
    if spec is None:
        return Decision("DENY", "unknown_tool")
    if ctx.tool_calls_used >= settings.max_tool_calls:
        return Decision("DENY", "budget")
    try:
        args = spec.args_model.model_validate(call.arguments)
    except ValidationError as e:
        first = e.errors()[0]
        return Decision("DENY", f"invalid_args: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
    if ctx.state is ConversationState.ESCALATED and spec.kind in ("write", "confirm"):
        return Decision("DENY", "escalated_no_writes", args)
    if spec.kind == "confirm":
        pending = store.get_pending(ctx.conversation_id)
        if pending is None:
            return Decision("DENY", "no_pending", args)
        if pending["token"] != args.confirmation_token:
            return Decision("DENY", "token_mismatch", args)
        if datetime.fromisoformat(pending["expires_at"]) < ctx.now:
            return Decision("DENY", "expired", args)
        if pending["turn_created"] >= ctx.turn_index:
            return Decision("DENY", "same_turn", args)
        return Decision("ALLOW", None, args)
    appointment_id = getattr(args, "appointment_id", None)
    if appointment_id is not None:
        appt = store.appointment(appointment_id)
        if appt is None or appt["patient_id"] != ctx.patient_id:
            return Decision("DENY", "not_owner", args)
    if spec.kind == "write":
        return Decision("PENDING", None, args)
    return Decision("ALLOW", None, args)

import secrets
from datetime import timedelta


@dataclass
class ToolOutcome:
    call_id: str
    tool: str
    status: ActionStatus
    output: dict[str, Any]
    summary: str
    verified: bool | None = None
    failure: FailureCode | None = None
    appointment_id: str | None = None
    idempotency_key: str | None = None
    reason: str | None = None
    escalation_id: str | None = None
    executed_tool: str | None = None


def execute(ctx: TurnContext, settings: Settings, store: Store, tools: HospitalTools,
            call: ToolCall, decision: Decision) -> ToolOutcome:
    ctx.tool_calls_used += 1
    if decision.kind == "DENY":
        out = ToolOutcome(call.call_id, call.name, ActionStatus.denied,
                          {"error": "denied", "reason": decision.reason},
                          summary=f"{call.name} denied: {decision.reason}", reason=decision.reason)
        return _audited(store, ctx, "DENY", out)
    args = decision.args
    kind = TOOL_SPECS[call.name].kind
    if decision.kind == "PENDING":
        return _create_pending(ctx, settings, store, tools, call, args)
    if kind == "confirm":
        return _confirm(ctx, store, tools, call)
    try:
        result = tools.call(call.name, ctx.patient_id, ctx.conversation_id, args)
    except (ToolError, ToolMalformed) as e:
        return _tool_failure(ctx, store, call, e)
    if kind == "escalation":
        store.set_state(ctx.conversation_id, ConversationState.ESCALATED.value)
        ctx.state = ConversationState.ESCALATED
        out = ToolOutcome(call.call_id, call.name, ActionStatus.executed, result.model_dump(),
                          summary=f"Escalated to a human agent ({result.escalation_id})",
                          escalation_id=result.escalation_id)
        return _audited(store, ctx, "ALLOW", out)
    out = ToolOutcome(call.call_id, call.name, ActionStatus.executed, result.model_dump(),
                      summary=f"{call.name} executed")
    return _audited(store, ctx, "ALLOW", out)


def _create_pending(ctx, settings, store, tools, call, args) -> ToolOutcome:
    try:
        summary = tools.describe(ctx.patient_id, call.name, args)
    except NotOwner:
        return _audited(store, ctx, "DENY", _denied(call, "not_owner"))
    except SlotUnavailable as e:
        return _audited(store, ctx, "DENY", _denied(call, f"slot_unavailable: {e}"))
    except AppointmentNotActive as e:
        return _audited(store, ctx, "DENY", _denied(call, f"appointment_not_active: {e}"))
    except DepartmentLimit as e:
        return _audited(store, ctx, "DENY", _denied(call, f"department_limit: {e}"))
    token = secrets.token_urlsafe(16)
    plain = args.model_dump()
    expires_at = (ctx.now + timedelta(seconds=settings.pending_ttl_s)).isoformat()
    store.set_pending(ctx.conversation_id, token=token, tool=call.name, args=plain, args_hash=args_hash(plain),
                      summary=summary, turn_created=ctx.turn_index, expires_at=expires_at)
    store.set_state(ctx.conversation_id, ConversationState.AWAITING_CONFIRMATION.value)
    ctx.state = ConversationState.AWAITING_CONFIRMATION
    output = {
        "status": "pending_confirmation",
        "confirmation_token": token,
        "summary": summary,
        "expires_at": expires_at,
        "instruction": ("Restate this action to the patient and ask them to confirm. "
                        "Do not call confirm_pending_action in this turn; it only works in a later message."),
    }
    out = ToolOutcome(call.call_id, call.name, ActionStatus.pending_confirmation, output, summary=summary,
                      appointment_id=getattr(args, "appointment_id", None))
    return _audited(store, ctx, "PENDING", out, args_hash=args_hash(plain))


def _confirm(ctx, store, tools, call) -> ToolOutcome:
    pending = store.get_pending(ctx.conversation_id)
    tool = pending["tool"]
    key = hashlib.sha256(
        f"{ctx.conversation_id}|{tool}|{pending['args_hash']}|{pending['token']}".encode()).hexdigest()
    appointment_id = pending["args"].get("appointment_id")
    prior = store.ledger_get(key)
    if prior is not None:  # duplicate confirmation: replay, never re-execute
        store.close_pending(ctx.conversation_id, "used")
        status = ActionStatus.executed_verified if prior["verified"] else ActionStatus.failed
        out = ToolOutcome(call.call_id, call.name, status, {**prior["result"], "verified": prior["verified"]},
                          summary=pending["summary"], verified=prior["verified"], appointment_id=appointment_id,
                          idempotency_key=key, executed_tool=tool,
                          failure=None if prior["verified"] else FailureCode.ACTION_UNVERIFIED)
        return _audited(store, ctx, "ALLOW", out, replay=True)
    write_args = TOOL_SPECS[tool].args_model.model_validate(pending["args"])
    try:
        result = tools.call(tool, ctx.patient_id, ctx.conversation_id, write_args)
    except (ToolError, ToolMalformed) as e:
        store.close_pending(ctx.conversation_id, "failed")
        store.set_state(ctx.conversation_id, ConversationState.IDLE.value)
        ctx.state = ConversationState.IDLE
        return _tool_failure(ctx, store, call, e, executed_tool=tool)
    except (NotOwner, SlotUnavailable, AppointmentNotActive, DepartmentLimit) as e:
        store.close_pending(ctx.conversation_id, "failed")
        store.set_state(ctx.conversation_id, ConversationState.IDLE.value)
        ctx.state = ConversationState.IDLE
        return _audited(store, ctx, "DENY", _denied(call, f"{type(e).__name__}: {e}"))
    verified = _verify(ctx, tools, tool, write_args, result)
    store.ledger_put(key, ctx.conversation_id, tool, pending["args_hash"], result.model_dump(), verified)
    store.close_pending(ctx.conversation_id, "used")
    if verified:
        store.set_state(ctx.conversation_id, ConversationState.IDLE.value)
        ctx.state = ConversationState.IDLE
        out = ToolOutcome(call.call_id, call.name, ActionStatus.executed_verified,
                          {**result.model_dump(), "verified": True}, summary=pending["summary"], verified=True,
                          appointment_id=appointment_id or getattr(result, "appointment_id", None),
                          idempotency_key=key, executed_tool=tool)
        return _audited(store, ctx, "ALLOW", out)
    escalation_id = None
    try:
        esc = tools.call("create_human_escalation", ctx.patient_id, ctx.conversation_id, CreateHumanEscalationArgs(
            reason_category=ReasonCategory.unverified_action,
            summary=f"{tool} reported success but read-back did not confirm it: {pending['summary']}"))
        escalation_id = esc.escalation_id
    except (ToolError, ToolMalformed):
        pass
    store.set_state(ctx.conversation_id, ConversationState.ESCALATED.value)
    ctx.state = ConversationState.ESCALATED
    out = ToolOutcome(call.call_id, call.name, ActionStatus.failed,
                      {"status": "unverified", "verified": False, "escalation_id": escalation_id,
                       "instruction": "The change could not be confirmed. Tell the patient nothing is confirmed "
                                      "and a human agent will follow up. Do not claim success."},
                      summary=pending["summary"], verified=False, failure=FailureCode.ACTION_UNVERIFIED,
                      appointment_id=appointment_id, idempotency_key=key, reason="read_back_mismatch",
                      escalation_id=escalation_id, executed_tool=tool)
    return _audited(store, ctx, "ALLOW", out)


def _verify(ctx, tools, tool, args, result) -> bool:
    """Read back the patient's appointments and check the write's post-condition."""
    try:
        appts = tools.call("get_patient_appointments", ctx.patient_id, ctx.conversation_id,
                           GetPatientAppointmentsArgs()).appointments
    except (ToolError, ToolMalformed):
        return False
    by_id = {a.appointment_id: a for a in appts}
    if tool == "book_appointment":
        a = by_id.get(result.appointment_id)
        return bool(a and a.start_time == result.start_time and a.status == "booked")
    if tool == "reschedule_appointment":
        a = by_id.get(args.appointment_id)
        return bool(a and a.start_time == result.new_start_time)
    if tool == "cancel_appointment":
        a = by_id.get(args.appointment_id)
        return bool(a and a.status == "cancelled")
    return False


def _tool_failure(ctx, store, call, exc, executed_tool=None) -> ToolOutcome:
    code = FailureCode.TOOL_MALFORMED if isinstance(exc, ToolMalformed) else FailureCode.TOOL_UNAVAILABLE
    out = ToolOutcome(call.call_id, call.name, ActionStatus.failed,
                      {"error": code.value,
                       "instruction": "Do not claim this succeeded. Tell the patient the hospital system is "
                                      "temporarily unavailable and offer a human agent."},
                      summary=f"{call.name} failed: {code.value}", failure=code, reason=code.value,
                      executed_tool=executed_tool)
    return _audited(store, ctx, "ALLOW", out)


def _denied(call, reason) -> ToolOutcome:
    return ToolOutcome(call.call_id, call.name, ActionStatus.denied, {"error": "denied", "reason": reason},
                       summary=f"{call.name} denied: {reason}", reason=reason)


def _audited(store, ctx, decision, out: ToolOutcome, **extra) -> ToolOutcome:
    store.audit(request_id=ctx.request_id, conversation_id=ctx.conversation_id, patient_hash=ctx.patient_hash,
                event="tool_call", tool=out.tool, decision=decision, status=out.status.value, verified=out.verified,
                failure=out.failure.value if out.failure else None, reason=out.reason,
                idempotency_key=out.idempotency_key, executed_tool=out.executed_tool, **extra)
    return out
