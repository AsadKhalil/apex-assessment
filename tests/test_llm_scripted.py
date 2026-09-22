import json

import pytest

from app.llm import LLMUnavailable, ScriptedLLM, calls, final, tool_schemas
from app.schemas import AssistantOutput


def test_tool_schemas_are_strict_and_never_mention_patient_id():
    schemas = tool_schemas()
    assert len(schemas) == 7
    assert all(s["type"] == "function" and s["strict"] is True for s in schemas)
    assert all(s["parameters"]["additionalProperties"] is False for s in schemas)
    assert "patient_id" not in json.dumps(schemas)
    by_name = {s["name"]: s for s in schemas}
    assert set(by_name["reschedule_appointment"]["parameters"]["required"]) == {"appointment_id", "new_slot_id"}


def test_scripted_returns_in_order_and_records_calls():
    llm = ScriptedLLM([calls(("get_patient_appointments", {})), final("Here are your appointments.")])
    r1 = llm.respond("sys", [{"role": "user", "content": "hi"}], tool_schemas(), AssistantOutput)
    assert r1.tool_calls[0].name == "get_patient_appointments" and r1.final is None
    assert r1.output_items[0]["type"] == "function_call" and r1.output_items[0]["call_id"] == "call_1"
    r2 = llm.respond("sys", [], tool_schemas(), AssistantOutput)
    assert r2.final.message == "Here are your appointments." and r2.tool_calls == []
    assert len(llm.calls) == 2 and llm.calls[0]["instructions"] == "sys"


def test_scripted_raises_scripted_exceptions_and_on_exhaustion():
    llm = ScriptedLLM([LLMUnavailable("down")])
    with pytest.raises(LLMUnavailable):
        llm.respond("sys", [], [], AssistantOutput)
    with pytest.raises(AssertionError):
        llm.respond("sys", [], [], AssistantOutput)


def test_callable_step_sees_the_transcript():
    llm = ScriptedLLM([lambda items: final(f"saw {len(items)} items")])
    assert llm.respond("sys", [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], [], AssistantOutput).final.message == "saw 2 items"


def test_final_helper_builds_valid_output():
    r = final("ok", intents=("book",), claimed=(("book_appointment", "A-1001-3"),), language="ar")
    assert r.final.language == "ar" and r.final.claimed_actions[0].appointment_id == "A-1001-3"
    assert r.final.intents[0].value == "book"
