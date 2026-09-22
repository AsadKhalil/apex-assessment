"""One conversation turn: retrieve, loop the model through the policy gate, guard the output, persist."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.llm import LLMClient, LLMOutputInvalid, LLMResult, LLMUnavailable, tool_schemas
from app.policy import ToolCall, ToolOutcome, TurnContext, authorize, execute
from app.prompts import (
    no_retrieved_note,
    pending_note,
    system_instructions,
    t,
    verified_success,
    wrap_retrieved,
)
from app.retrieval import RetrievalResult, Retriever
from app.schemas import (
    ActionRecord,
    ActionStatus,
    AssistantOutput,
    AssistantResponse,
    ConversationState,
    Failure,
    FailureCode,
    PendingConfirmation,
    Source,
)
from app.state import Store
from app.tools import HospitalTools

log = logging.getLogger("assistant")
RETRY_BACKOFF_S = 0.2
_ARABIC = re.compile(r"[؀-ۿ]")
_BLOCKING_DENIALS = {"not_owner", "escalated_no_writes"}


@dataclass
class Deps:
    settings: Settings
    store: Store
    retriever: Retriever
    llm: LLMClient

    def tools(self, fault: str | None) -> HospitalTools:
        return HospitalTools(self.store, self.settings, fault)


def detect_lang(message: str, locale: str | None) -> str:
    if locale in ("en", "ar"):
        return locale
    return "ar" if _ARABIC.search(message) else "en"


def build_input(history: list[dict], retrieved: RetrievalResult, pending: dict | None, message: str) -> list[dict]:
    items = [{"role": m["role"], "content": m["content"]} for m in history]
    notes = [wrap_retrieved(retrieved.chunks) if retrieved.status == "ok" else no_retrieved_note()]
    if pending:
        notes.append(pending_note(pending))
    items.append({"role": "developer", "content": "\n".join(notes)})
    items.append({"role": "user", "content": message})
    return items


def call_model(llm: LLMClient, instructions: str, input_items: list) -> LLMResult:
    try:
        return llm.respond(instructions, input_items, tool_schemas(), AssistantOutput)
    except (LLMUnavailable, LLMOutputInvalid):
        time.sleep(RETRY_BACKOFF_S)
        return llm.respond(instructions, input_items, tool_schemas(), AssistantOutput)


def run_turn(deps: Deps, *, patient_id: str, patient_hash: str, conversation_id: str, message: str,
             request_id: str, locale: str | None = None, fault: str | None = None) -> AssistantResponse:
    store, settings = deps.store, deps.settings
    conv = store.get_conversation(conversation_id)
    turn_index = store.next_turn(conversation_id)
    ctx = TurnContext(request_id, conversation_id, patient_id, patient_hash, turn_index,
                      ConversationState(conv["state"]), store.now())
    tools = deps.tools(fault)
    started = time.perf_counter()
    store.audit(request_id=request_id, conversation_id=conversation_id, patient_hash=patient_hash,
                event="turn_started", turn=turn_index, state=ctx.state.value, fault=fault)

    history = store.list_messages(conversation_id)
    store.append_message(conversation_id, "user", message)
    retrieved = deps.retriever.search(message)
    pending_before = store.get_pending(conversation_id)
    input_items = build_input(history, retrieved, pending_before, message)
    instructions = system_instructions(ctx.now.strftime("%Y-%m-%d"))

    outcomes: list[ToolOutcome] = []
    final: AssistantOutput | None = None
    failure: FailureCode | None = None
    tokens_in = tokens_out = 0
    for _ in range(settings.max_iterations):
        ctx.iterations_used += 1
        try:
            result = call_model(deps.llm, instructions, input_items)
        except LLMUnavailable:
            failure = FailureCode.MODEL_UNAVAILABLE
            break
        except LLMOutputInvalid:
            failure = FailureCode.MODEL_OUTPUT_INVALID
            break
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        if not result.tool_calls:
            final = result.final
            if final is None:
                failure = FailureCode.MODEL_OUTPUT_INVALID
            break
        input_items.extend(result.output_items)
        for tc in result.tool_calls:
            call = ToolCall(tc.call_id, tc.name, tc.arguments)
            outcome = execute(ctx, settings, store, tools, call, authorize(ctx, settings, store, call))
            outcomes.append(outcome)
            input_items.append({"type": "function_call_output", "call_id": tc.call_id,
                                "output": json.dumps(outcome.output, default=str)})
            if outcome.reason == "budget":
                failure = FailureCode.BUDGET_EXCEEDED
        if failure is FailureCode.BUDGET_EXCEEDED:
            break
    else:
        failure = FailureCode.BUDGET_EXCEEDED

    lang = final.language if final is not None else detect_lang(message, locale)
    response = compose(ctx, store, retrieved, outcomes, final, failure, lang)
    store.append_message(conversation_id, "assistant", response.message)
    latency_ms = int((time.perf_counter() - started) * 1000)
    store.audit(request_id=request_id, conversation_id=conversation_id, patient_hash=patient_hash,
                event="turn_completed", state=ctx.state.value,
                failure=response.failure.code.value if response.failure else None,
                actions=[(a.tool, a.status.value) for a in response.actions],
                retrieval=retrieved.status, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms)
    log.info("turn_completed", extra={"request_id": request_id, "conversation_id": conversation_id,
                                      "patient_hash": patient_hash, "turn_index": turn_index,
                                      "state": ctx.state.value, "latency_ms": latency_ms,
                                      "tokens_in": tokens_in, "tokens_out": tokens_out,
                                      "failure_code": response.failure.code.value if response.failure else None,
                                      "tools": [a.tool for a in response.actions]})
    return response


def compose(ctx: TurnContext, store: Store, retrieved: RetrievalResult, outcomes: list[ToolOutcome],
            final: AssistantOutput | None, failure: FailureCode | None, lang: str) -> AssistantResponse:
    """The output guard: the model's text is used only when it is consistent with the verified ledger."""
    verified = [o for o in outcomes if o.status is ActionStatus.executed_verified]
    unverified = [o for o in outcomes if o.failure is FailureCode.ACTION_UNVERIFIED]
    pendings = [o for o in outcomes if o.status is ActionStatus.pending_confirmation]
    tool_failed = [o for o in outcomes if o.failure in (FailureCode.TOOL_UNAVAILABLE, FailureCode.TOOL_MALFORMED)]
    blocked = [o for o in outcomes if o.status is ActionStatus.denied and o.reason in _BLOCKING_DENIALS]
    escalation_id = next((o.escalation_id for o in reversed(outcomes) if o.escalation_id), None)

    if failure in (FailureCode.MODEL_UNAVAILABLE, FailureCode.MODEL_OUTPUT_INVALID):
        message = t("model_unavailable", lang)
    elif failure is FailureCode.BUDGET_EXCEEDED:
        message = t("budget_exceeded", lang)
    elif unverified:
        failure = FailureCode.ACTION_UNVERIFIED
        message = t("unverified", lang, escalation_id=unverified[-1].escalation_id or "pending")
    else:
        base = final.message if final is not None else ""
        declined = False
        if (final is not None and final.pending_declined and not pendings and not verified
                and ctx.state is ConversationState.AWAITING_CONFIRMATION):
            store.close_pending(ctx.conversation_id, "declined")
            store.set_state(ctx.conversation_id, ConversationState.IDLE.value)
            ctx.state = ConversationState.IDLE
            declined = True
        claimed = {(c.tool, c.appointment_id) for c in (final.claimed_actions if final else [])}
        allowed = {(o.executed_tool, o.appointment_id) for o in verified} | {(o.executed_tool, None) for o in verified}
        if not claimed <= allowed:
            store.audit(request_id=ctx.request_id, conversation_id=ctx.conversation_id, patient_hash=ctx.patient_hash,
                        event="claim_mismatch", claimed=sorted(map(str, claimed)), allowed=sorted(map(str, allowed)))
            base = ""
        lines = [verified_success(o.executed_tool, o.output, _department(store, o), lang) for o in verified]
        if tool_failed and not verified and not pendings:
            failure = tool_failed[-1].failure
            base = t("tool_unavailable", lang)
        elif blocked and not verified and not pendings:
            failure = FailureCode.POLICY_DENIED
        if pendings:
            p = pendings[-1]
            lines.append(t("confirmation", lang, summary=p.summary,
                           expires=datetime.fromisoformat(p.output["expires_at"]).strftime("%H:%M")))
        if declined:
            lines.append(t("declined", lang))
        message = "\n\n".join(part for part in [base, *lines] if part) or t("nothing_changed", lang)

    pending_row = store.get_pending(ctx.conversation_id) if ctx.state is ConversationState.AWAITING_CONFIRMATION else None
    return AssistantResponse(
        conversation_id=ctx.conversation_id,
        request_id=ctx.request_id,
        message=message,
        state=ctx.state,
        intents=list(final.intents) if final is not None else [],
        actions=[ActionRecord(tool=o.executed_tool or o.tool, status=o.status, summary=o.summary,
                              appointment_id=o.appointment_id, idempotency_key=o.idempotency_key, reason=o.reason)
                 for o in outcomes],
        sources=[Source(doc_id=c.doc_id, title=c.title, chunk_id=c.chunk_id) for c in retrieved.chunks],
        pending_confirmation=PendingConfirmation(token=pending_row["token"], summary=pending_row["summary"],
                                                expires_at=pending_row["expires_at"]) if pending_row else None,
        escalation_id=escalation_id,
        failure=Failure(code=failure, message=t("nothing_changed", lang)) if failure else None,
    )


def _department(store: Store, outcome: ToolOutcome) -> str:
    appt = store.appointment(outcome.output.get("appointment_id", ""))
    return appt["department"] if appt else ""
