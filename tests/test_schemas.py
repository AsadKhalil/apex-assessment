import pytest
from pydantic import ValidationError

from app.schemas import (
    AssistantOutput,
    AssistantResponse,
    BookAppointmentArgs,
    ConversationState,
    GetAvailableSlotsArgs,
    strict_schema,
)


def test_args_forbid_extra_fields():
    with pytest.raises(ValidationError):
        BookAppointmentArgs(slot_id="S-1", patient_id="P-1001")


def test_strict_schema_marks_everything_required_and_closed():
    schema = strict_schema(GetAvailableSlotsArgs)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"department", "date_from", "date_to", "clinician"}
    clinician = schema["properties"]["clinician"]
    assert {"type": "null"} in clinician["anyOf"]
    assert "default" not in clinician
    assert "title" not in schema


def test_strict_schema_nested_objects_are_closed():
    schema = strict_schema(AssistantOutput)
    claimed = schema["$defs"]["ClaimedAction"]
    assert claimed["additionalProperties"] is False
    assert set(claimed["required"]) == {"tool", "appointment_id"}


def test_assistant_response_round_trip():
    payload = {
        "conversation_id": "c1",
        "request_id": "r1",
        "message": "hi",
        "state": "IDLE",
        "intents": ["information"],
        "actions": [],
        "sources": [],
        "pending_confirmation": None,
        "escalation_id": None,
        "failure": None,
    }
    r = AssistantResponse.model_validate(payload)
    assert r.state is ConversationState.IDLE
    assert AssistantResponse.model_validate(r.model_dump(mode="json")) == r
