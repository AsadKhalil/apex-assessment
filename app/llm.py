"""LLM client protocol, strict tool schemas, and the scripted fake used by tests and fake-mode evals."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.schemas import AssistantOutput, ClaimedAction, Intent, strict_schema
from app.tools import TOOL_SPECS


class LLMUnavailable(Exception):
    """Transport or provider failure after retry. Maps to MODEL_UNAVAILABLE."""


class LLMOutputInvalid(Exception):
    """The final answer did not match AssistantOutput. Maps to MODEL_OUTPUT_INVALID."""


@dataclass(frozen=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResult:
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    final: AssistantOutput | None = None
    output_items: list[Any] = field(default_factory=list)  # replayed into the next request's input
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def respond(self, instructions: str, input_items: list[Any], tools: list[dict],
                text_format: type[AssistantOutput]) -> LLMResult: ...


def tool_schemas() -> list[dict]:
    return [
        {"type": "function", "name": spec.name, "description": spec.description, "strict": True,
         "parameters": strict_schema(spec.args_model)}
        for spec in TOOL_SPECS.values()
    ]


class ScriptedLLM:
    """Returns scripted results in order. An Exception entry is raised. Running out is a test bug."""

    def __init__(self, steps: list):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def respond(self, instructions, input_items, tools, text_format) -> LLMResult:
        self.calls.append({"instructions": instructions, "input_items": list(input_items), "tools": tools})
        if not self.steps:
            raise AssertionError("ScriptedLLM: script exhausted")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):  # a step may compute its result from the transcript (e.g. read a token)
            step = step(list(input_items))
        if step.tool_calls and not step.output_items:
            step.output_items = [
                {"type": "function_call", "call_id": c.call_id, "name": c.name, "arguments": json.dumps(c.arguments)}
                for c in step.tool_calls
            ]
        return step


def calls(*pairs: tuple[str, dict]) -> LLMResult:
    return LLMResult(tool_calls=[LLMToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(pairs, 1)])


def final(message: str, intents=("information",), claimed=(), language="en",
          escalation_recommended=False, pending_declined=False) -> LLMResult:
    return LLMResult(final=AssistantOutput(
        message=message, intents=[Intent(i) for i in intents],
        claimed_actions=[ClaimedAction(tool=t, appointment_id=a) for t, a in claimed],
        escalation_recommended=escalation_recommended, pending_declined=pending_declined, language=language))


from pydantic import ValidationError

from app.config import Settings


class OpenAIResponsesClient:
    """Thin, stateless adapter over the Responses API. The agent replays output items between calls."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key, timeout=30.0, max_retries=0)
        self.client = client

    def respond(self, instructions, input_items, tools, text_format) -> LLMResult:
        import openai

        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model, "instructions": instructions, "input": input_items,
            "tools": tools, "text_format": text_format, "store": False,
            "include": ["reasoning.encrypted_content"]}
        if self.settings.openai_reasoning_effort:
            kwargs["reasoning"] = {"effort": self.settings.openai_reasoning_effort}
        try:
            resp = self.client.responses.parse(**kwargs)
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError) as e:
            raise LLMUnavailable(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMUnavailable(str(e)) from e
            raise
        except (ValidationError, ValueError) as e:  # final text did not match AssistantOutput
            raise LLMOutputInvalid(str(e)) from e
        return to_result(resp)


def to_result(resp) -> LLMResult:
    tool_calls: list[LLMToolCall] = []
    for item in resp.output:
        if getattr(item, "type", None) == "function_call":
            try:
                arguments = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": item.arguments}
            tool_calls.append(LLMToolCall(item.call_id, item.name, arguments))
    final = None
    if not tool_calls:
        final = getattr(resp, "output_parsed", None)
        if final is None:
            raise LLMOutputInvalid("no parsed final output (refusal or schema mismatch)")
    usage = getattr(resp, "usage", None)
    return LLMResult(
        tool_calls=tool_calls,
        final=final,
        output_items=[item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump")
                      else (vars(item) if not isinstance(item, dict) else item) for item in resp.output],
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
