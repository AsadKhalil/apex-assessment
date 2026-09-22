"""All data contracts: enums, tool args/results, model output, and the API."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationState(str, Enum):
    IDLE = "IDLE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    ESCALATED = "ESCALATED"


class Intent(str, Enum):
    book = "book"
    reschedule = "reschedule"
    cancel = "cancel"
    availability = "availability"
    information = "information"
    escalation = "escalation"
    other = "other"


class FailureCode(str, Enum):
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_MALFORMED = "TOOL_MALFORMED"
    ACTION_UNVERIFIED = "ACTION_UNVERIFIED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    POLICY_DENIED = "POLICY_DENIED"


class ActionStatus(str, Enum):
    executed = "executed"
    pending_confirmation = "pending_confirmation"
    executed_verified = "executed_verified"
    denied = "denied"
    failed = "failed"


class ReasonCategory(str, Enum):
    user_request = "user_request"
    emergency = "emergency"
    unverified_action = "unverified_action"
    tool_failure = "tool_failure"
    out_of_scope = "out_of_scope"
    insurance_determination = "insurance_determination"
    other = "other"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- tool arguments (model-facing; never contain patient_id) ----
class GetPatientAppointmentsArgs(StrictModel):
    pass


class GetAvailableSlotsArgs(StrictModel):
    department: str = Field(description="Department name, e.g. cardiology, dermatology, dental, radiology")
    date_from: str = Field(description="Inclusive start date, YYYY-MM-DD")
    date_to: str = Field(description="Inclusive end date, YYYY-MM-DD")
    clinician: str | None = Field(default=None, description="Optional clinician name filter")


class BookAppointmentArgs(StrictModel):
    slot_id: str = Field(description="A slot_id returned by get_available_slots")
    reason: str | None = Field(default=None, description="Optional short reason for the visit")


class RescheduleAppointmentArgs(StrictModel):
    appointment_id: str = Field(description="One of the patient's own appointment ids")
    new_slot_id: str = Field(description="A slot_id returned by get_available_slots")


class CancelAppointmentArgs(StrictModel):
    appointment_id: str = Field(description="One of the patient's own appointment ids")
    reason: str | None = Field(default=None, description="Optional cancellation reason")


class CreateHumanEscalationArgs(StrictModel):
    reason_category: ReasonCategory
    summary: str = Field(description="One or two sentences for the human agent; no diagnosis")


class ConfirmPendingActionArgs(StrictModel):
    confirmation_token: str = Field(description="The confirmation_token from a pending_confirmation result")


# ---- tool results (validated on return) ----
class Appointment(StrictModel):
    appointment_id: str
    department: str
    clinician: str
    start_time: str
    status: str
    location: str


class Slot(StrictModel):
    slot_id: str
    department: str
    clinician: str
    start_time: str


class AppointmentsResult(StrictModel):
    appointments: list[Appointment]


class SlotsResult(StrictModel):
    slots: list[Slot]


class BookResult(StrictModel):
    appointment_id: str
    slot_id: str
    start_time: str
    status: Literal["booked"]


class RescheduleResult(StrictModel):
    appointment_id: str
    old_start_time: str
    new_start_time: str
    status: Literal["rescheduled"]


class CancelResult(StrictModel):
    appointment_id: str
    status: Literal["cancelled"]


class EscalationResult(StrictModel):
    escalation_id: str
    status: Literal["open"]


# ---- the model's final structured answer ----
class ClaimedAction(StrictModel):
    tool: str
    appointment_id: str | None = None


class AssistantOutput(StrictModel):
    message: str
    intents: list[Intent]
    claimed_actions: list[ClaimedAction]
    escalation_recommended: bool
    pending_declined: bool  # true only when the patient declined the pending action in this message
    language: Literal["en", "ar"]


# ---- API ----
class MessageRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    locale: Literal["en", "ar"] | None = None


class ActionRecord(BaseModel):
    tool: str
    status: ActionStatus
    summary: str
    appointment_id: str | None = None
    idempotency_key: str | None = None
    reason: str | None = None


class Source(BaseModel):
    doc_id: str
    title: str
    chunk_id: str


class PendingConfirmation(BaseModel):
    token: str
    summary: str
    expires_at: str


class Failure(BaseModel):
    code: FailureCode
    message: str


class AssistantResponse(BaseModel):
    conversation_id: str
    request_id: str
    message: str
    state: ConversationState
    intents: list[Intent]
    actions: list[ActionRecord]
    sources: list[Source]
    pending_confirmation: PendingConfirmation | None = None
    escalation_id: str | None = None
    failure: Failure | None = None


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema in OpenAI strict form: every property required, objects closed, no defaults."""

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return {k: fix(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(model.model_json_schema())
