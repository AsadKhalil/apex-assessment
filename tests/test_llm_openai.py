from types import SimpleNamespace

import httpx
import openai
import pytest

from app.config import Settings
from app.llm import LLMOutputInvalid, LLMUnavailable, OpenAIResponsesClient, tool_schemas
from app.schemas import AssistantOutput


class FakeResponses:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.kwargs = resp, exc, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.resp


def fake_client(resp=None, exc=None):
    return SimpleNamespace(responses=FakeResponses(resp, exc))


def settings():
    return Settings.from_env(openai_api_key="sk-test", openai_model="gpt-5.6-luna", openai_reasoning_effort="low")


def test_request_is_stateless_and_strict():
    call = SimpleNamespace(type="function_call", call_id="call_abc", name="get_patient_appointments", arguments="{}")
    resp = SimpleNamespace(output=[call], output_parsed=None, usage=SimpleNamespace(input_tokens=120, output_tokens=8))
    client = fake_client(resp)
    r = OpenAIResponsesClient(settings(), client=client).respond("sys", [{"role": "user", "content": "hi"}], tool_schemas(), AssistantOutput)
    k = client.responses.kwargs
    assert k["model"] == "gpt-5.6-luna" and k["store"] is False and k["include"] == ["reasoning.encrypted_content"]
    assert k["instructions"] == "sys" and k["text_format"] is AssistantOutput and k["reasoning"] == {"effort": "low"}
    assert all(t["strict"] is True for t in k["tools"])
    assert r.tool_calls[0].call_id == "call_abc" and r.tool_calls[0].arguments == {}
    assert r.input_tokens == 120 and r.output_tokens == 8 and r.final is None
    assert r.output_items[0]["type"] == "function_call"


def test_final_answer_is_parsed():
    parsed = AssistantOutput(message="hi", intents=[], claimed_actions=[], escalation_recommended=False,
                             pending_declined=False, language="en")
    resp = SimpleNamespace(output=[SimpleNamespace(type="message")], output_parsed=parsed, usage=None)
    r = OpenAIResponsesClient(settings(), client=fake_client(resp)).respond("sys", [], [], AssistantOutput)
    assert r.final is parsed and r.tool_calls == []


def test_unparseable_final_is_invalid_output():
    resp = SimpleNamespace(output=[SimpleNamespace(type="message")], output_parsed=None, usage=None)
    with pytest.raises(LLMOutputInvalid):
        OpenAIResponsesClient(settings(), client=fake_client(resp)).respond("sys", [], [], AssistantOutput)


def test_transport_errors_become_unavailable():
    exc = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    with pytest.raises(LLMUnavailable):
        OpenAIResponsesClient(settings(), client=fake_client(exc=exc)).respond("sys", [], [], AssistantOutput)
    resp500 = httpx.Response(503, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    exc2 = openai.APIStatusError("down", response=resp500, body=None)
    with pytest.raises(LLMUnavailable):
        OpenAIResponsesClient(settings(), client=fake_client(exc=exc2)).respond("sys", [], [], AssistantOutput)
    resp401 = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    exc3 = openai.APIStatusError("bad key", response=resp401, body=None)
    with pytest.raises(LLMUnavailable):  # provider rejection must not surface as a 500
        OpenAIResponsesClient(settings(), client=fake_client(exc=exc3)).respond("sys", [], [], AssistantOutput)


def test_malformed_arguments_are_not_swallowed():
    call = SimpleNamespace(type="function_call", call_id="c1", name="cancel_appointment", arguments="{not json")
    resp = SimpleNamespace(output=[call], output_parsed=None, usage=None)
    r = OpenAIResponsesClient(settings(), client=fake_client(resp)).respond("sys", [], [], AssistantOutput)
    assert r.tool_calls[0].arguments == {"_raw": "{not json"}  # the gate rejects this as invalid_args
