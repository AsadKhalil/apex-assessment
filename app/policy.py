"""The gate. Every tool call the model proposes passes through authorize() and then execute()."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas import ConversationState
from app.state import Store
from app.tools import TOOL_SPECS


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
