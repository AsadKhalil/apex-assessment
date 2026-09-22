"""Eval runner. fake mode drives the scripted model (CI); openai mode drives the real model (evidence)."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.agent import Deps, run_turn
from app.config import Settings
from app.llm import LLMOutputInvalid, LLMResult, LLMToolCall, LLMUnavailable, ScriptedLLM
from app.retrieval import Retriever
from app.schemas import AssistantOutput, AssistantResponse, ClaimedAction, Intent
from app.state import Store

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases.yaml"
REPORTS = ROOT / "evals" / "reports"
FIXED_NOW = "2026-10-05T09:00:00+03:00"
# USD per 1M tokens (input, output), OpenAI pricing page, verified 2026-09-22. Cached-input discounts are not applied.
PRICES = {"gpt-5.6-luna": (0.20, 1.20), "gpt-5.6-terra": (2.00, 12.00), "gpt-5.6-sol": (4.00, 20.00),
          "gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40), "gpt-4.1-mini": (0.40, 1.60)}


@dataclass
class TurnResult:
    user: str
    passed: bool
    problems: list[str]
    latency_ms: int
    tokens_in: int
    tokens_out: int
    message: str


@dataclass
class CaseResult:
    id: str
    category: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.turns)


def load_cases() -> list[dict]:
    return yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]


def _token_from_items(items) -> str | None:
    """The pending action's token from the current turn's transcript (same-turn confirm attempts)."""
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            try:
                data = json.loads(item["output"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and "confirmation_token" in data:
                return data["confirmation_token"]
    return None


def script_step(step: dict, token: str | None):
    if "error" in step:
        return {"unavailable": LLMUnavailable("scripted"), "invalid_output": LLMOutputInvalid("scripted")}[step["error"]]
    if "tool_calls" in step:
        raw = [dict(c) for c in step["tool_calls"]]
        needs_token = any("{{token}}" in json.dumps(c.get("arguments", {})) for c in raw)
        if needs_token:
            def compute(items):
                live = _token_from_items(items) or token or "missing-token"
                calls = []
                for i, c in enumerate(raw, 1):
                    args = json.loads(json.dumps(c.get("arguments", {})).replace("{{token}}", live))
                    calls.append(LLMToolCall(f"call_{i}", c["name"], args))
                return LLMResult(tool_calls=calls)
            return compute
        calls = []
        for i, c in enumerate(raw, 1):
            calls.append(LLMToolCall(f"call_{i}", c["name"], dict(c.get("arguments", {}))))
        return LLMResult(tool_calls=calls)
    f = step["final"]
    return LLMResult(final=AssistantOutput(
        message=f["message"], intents=[Intent(i) for i in f.get("intents", ["information"])],
        claimed_actions=[ClaimedAction(**c) for c in f.get("claimed_actions", [])],
        escalation_recommended=bool(f.get("escalation_recommended", False)),
        pending_declined=bool(f.get("pending_declined", False)), language=f.get("language", "en")))


def is_subsequence(needle: list, haystack: list) -> bool:
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def reason_matches(expected: str, reasons: set[str]) -> bool:
    """A denial reason matches exactly or as a 'code: detail' prefix (e.g. department_limit: ...)."""
    return any(r == expected or r.startswith(expected + ":") for r in reasons)


def check(expect: dict, resp: AssistantResponse, store: Store) -> list[str]:
    problems: list[str] = []
    seq = [a.tool for a in resp.actions]
    executed = {a.tool for a in resp.actions if a.status.value in ("executed", "executed_verified")}
    verified = [a.tool for a in resp.actions if a.status.value == "executed_verified"]
    pending = [a.tool for a in resp.actions if a.status.value == "pending_confirmation"]
    reasons = {a.reason for a in resp.actions if a.reason}
    msg = resp.message.lower()
    failure = resp.failure.code.value if resp.failure else None
    if "tools_called" in expect and not is_subsequence(expect["tools_called"], seq):
        problems.append(f"tools_called {expect['tools_called']} not in {seq}")
    for tool in expect.get("tools_not_called", []):
        if tool in executed:
            problems.append(f"{tool} was executed")
    if "state" in expect and resp.state.value != expect["state"]:
        problems.append(f"state {resp.state.value} != {expect['state']}")
    if "pending_action_tool" in expect and expect["pending_action_tool"] not in pending:
        problems.append(f"pending_action_tool {expect['pending_action_tool']} not in {pending}")
    if expect.get("no_pending") and resp.pending_confirmation is not None:
        problems.append("a pending action exists")
    if "actions_verified" in expect and not set(expect["actions_verified"]) <= set(verified):
        problems.append(f"actions_verified {expect['actions_verified']} not in {verified}")
    if expect.get("no_verified_writes") and verified:
        problems.append(f"verified writes happened: {verified}")
    if "failure_code" in expect and failure != expect["failure_code"]:
        problems.append(f"failure_code {failure} != {expect['failure_code']}")
    if "escalation_created" in expect and (resp.escalation_id is not None) != expect["escalation_created"]:
        problems.append(f"escalation_created is {resp.escalation_id is not None}")
    if "denied_reasons" in expect:
        missing = [r for r in expect["denied_reasons"] if not reason_matches(r, reasons)]
        if missing:
            problems.append(f"denied_reasons {missing} not in {sorted(reasons)}")
    if "must_include_any" in expect and not any(s.lower() in msg for s in expect["must_include_any"]):
        problems.append(f"message lacks any of {expect['must_include_any']}")
    for s in expect.get("must_not_include", []):
        if s.lower() in msg:
            problems.append(f"message contains forbidden {s!r}")
    docs = {s.doc_id for s in resp.sources}
    if "sources_include" in expect and not set(expect["sources_include"]) <= docs:
        problems.append(f"sources {sorted(docs)} lack {expect['sources_include']}")
    if expect.get("sources_empty") and docs:
        problems.append(f"sources not empty: {sorted(docs)}")
    for appt_id, status in expect.get("appointment_status", {}).items():
        row = store.appointment(appt_id)
        actual = row["status"] if row else None
        if actual != status:
            problems.append(f"{appt_id} status {actual} != {status}")
    return problems


def run_case(case: dict, mode: str, model: str | None = None) -> CaseResult:
    settings = Settings.from_env(db_path=":memory:", fake_now=FIXED_NOW, app_env="eval", tool_timeout_s=0.5,
                                 **({"openai_model": model} if model else {}))
    store = Store(settings)
    store.init_schema()
    store.seed()
    kb_dir = ROOT / case["kb"] if case.get("kb") else ROOT / "kb"
    retriever = Retriever(kb_dir, threshold=case.get("kb_threshold", 3.0), screen_enabled=case.get("kb_screen", True))
    if case.get("retrieval_error"):
        retriever.index.score = lambda q: (_ for _ in ()).throw(RuntimeError("index unavailable"))
    if mode == "fake":
        llm = ScriptedLLM([])
    else:
        from app.llm import OpenAIResponsesClient

        llm = OpenAIResponsesClient(settings)
    deps = Deps(settings, store, retriever, llm)
    cid = store.create_conversation(case["patient"])
    result = CaseResult(case["id"], case["category"])
    token: str | None = None
    for i, turn in enumerate(case["turns"], 1):
        if mode == "fake":
            llm.steps = [script_step(s, token) for s in turn["fake_llm"]]
        started = time.perf_counter()
        resp = run_turn(deps, patient_id=case["patient"], patient_hash="eval", conversation_id=cid,
                        message=turn["user"], request_id=f"{case['id']}-{i}", fault=case.get("fault"))
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.pending_confirmation:
            token = resp.pending_confirmation.token
        expect = {**turn.get("expect", {}), **turn.get(f"expect_{mode}", {})}
        problems = check(expect, resp, store)
        detail = store.list_audit(cid)[-1]["detail"]
        result.turns.append(TurnResult(turn["user"], not problems, problems, latency_ms,
                                       int(detail.get("tokens_in", 0)), int(detail.get("tokens_out", 0)), resp.message))
    return result


def write_report(results: list[CaseResult], mode: str, model: str | None, path: Path) -> Path:
    price_in, price_out = PRICES.get(model or "", (0.0, 0.0))
    rows = ["| Case | Category | Turn | Result | Problems | Latency ms | Tokens in/out | Est. cost USD |",
            "|---|---|---|---|---|---|---|---|"]
    latencies, total_cost, passed = [], 0.0, 0
    for r in results:
        passed += r.passed
        for i, t in enumerate(r.turns, 1):
            cost = t.tokens_in * price_in / 1e6 + t.tokens_out * price_out / 1e6
            total_cost += cost
            latencies.append(t.latency_ms)
            rows.append(f"| {r.id} | {r.category} | {i} | {'PASS' if t.passed else 'FAIL'} | "
                        f"{'; '.join(t.problems) or '-'} | {t.latency_ms} | {t.tokens_in}/{t.tokens_out} | {cost:.5f} |")
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r.passed)
    cat_rows = [f"| {c} | {sum(v)}/{len(v)} |" for c, v in sorted(by_cat.items())]
    p50 = int(statistics.median(latencies)) if latencies else 0
    p95 = int(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]) if latencies else 0
    text = "\n".join([
        f"# Eval report: mode={mode} model={model or '-'} date={date.today().isoformat()}",  # noqa: DTZ011
        "",
        f"Cases: {len(results)}. Passed: {passed}. Pass rate: {100 * passed / max(1, len(results)):.0f}%.",
        (f"Turn latency p50: {p50} ms, p95: {p95} ms. Estimated model cost for this run: ${total_cost:.4f} "
         f"(prices per 1M tokens: in ${price_in}, out ${price_out}; no cached-input discount applied)."),
        "",
        "## By category", "", "| Category | Passed |", "|---|---|", *cat_rows,
        "", "## Cases", "", *rows,
        "", "## Transcript excerpts", "",
        *[f"- **{r.id}** turn {i}: user: {t.user!r} -> assistant: {t.message[:300]!r}"
          for r in results for i, t in enumerate(r.turns, 1)],
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run evaluation cases.")
    parser.add_argument("--mode", choices=["fake", "openai"], default="fake")
    parser.add_argument("--ids", default="", help="comma-separated case ids (default: all for the mode)")
    parser.add_argument("--model", default=None, help="override OPENAI_MODEL for openai mode")
    args = parser.parse_args(argv)
    wanted = {i for i in args.ids.split(",") if i}
    cases = [c for c in load_cases() if args.mode in c["modes"] and (not wanted or c["id"] in wanted)]
    model = args.model or (Settings.from_env().openai_model if args.mode == "openai" else None)
    results = []
    for case in cases:
        result = run_case(case, args.mode, model)
        results.append(result)
        print(f"{'PASS' if result.passed else 'FAIL'} {case['id']}", flush=True)
        for i, t in enumerate(result.turns, 1):
            for p in t.problems:
                print(f"   turn {i}: {p}")
    suffix = f"-{model}" if model else ""
    path = write_report(results, args.mode, model, REPORTS / f"{date.today().isoformat()}-{args.mode}{suffix}.md")  # noqa: DTZ011
    print(f"report: {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
