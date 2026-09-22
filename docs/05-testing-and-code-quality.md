# Testing and code quality (Part 5)

## Test map

105 tests, all passing (`uv run pytest -q`). Types: unit, contract, flow (end-to-end with the scripted model), injection, runner.

| File | Tests | Type | Proves |
|---|---|---|---|
| tests/test_config.py | 5 | unit | settings from env, fixed Riyadh clock, inline-comment stripping |
| tests/test_schemas.py | 4 | contract | strict schemas (closed, all-required, no defaults), API round trip |
| tests/test_state.py | 7 | unit | seed data, Saudi work-week slots, pending replace/close, ledger, audit |
| tests/test_tools.py | 16 | unit + contract | tool registry; patient scoping; slot lifecycle; ownership; one-per-department rule; escalation idempotency; every fault mode; faults off in production |
| tests/test_policy.py | 9 | unit | every authorize() branch: unknown, budget, invalid args, pending, ownership, escalated, token rules |
| tests/test_policy_execute.py | 10 | unit | execution, verification, idempotent replay, silent-noop → unverified + escalation, fault mapping, department-limit denial |
| tests/test_llm_scripted.py | 5 | contract | tool schemas strict and free of patient_id; scripted fake behavior |
| tests/test_llm_openai.py | 5 | contract | request shape to the provider (store=False, strict tools, text_format), error mapping, malformed arguments |
| tests/test_prompts.py | 4 | unit | templates exist in both languages; verified-success text built from facts; instruction content |
| tests/test_retrieval.py | 10 | unit | manifest-only indexing, chunking, top hits, no-match, Arabic, poison screening, error containment |
| tests/test_agent_flows.py | 12 | flow | two-turn booking, unrelated message does not confirm, silent-noop incident path, claim-mismatch rewrite, budgets, model failures, tool failure template, sources, cross-patient denial, same-turn denial, decline, Arabic |
| tests/test_injection.py | 2 | injection | poisoned content that the model obeys still executes nothing; screen removes the poison |
| tests/test_logging.py | 4 | unit | redaction on both logging paths, request-id context, keyed patient hash |
| tests/test_api.py | 7 | contract | 401/403/404/422, response validates against `AssistantResponse`, fault header boundaries |
| tests/test_evals_runner.py | 5 | runner | cases well formed, every fake case passes, check() reports each unmet expectation, report writing |

## Contract tests

Four layers pin the contracts: `tests/test_schemas.py` (data contracts in strict form), `tests/test_tools.py` (tool results validated against their models), `tests/test_api.py` (HTTP responses validate against `AssistantResponse`), `tests/test_llm_openai.py` (the exact request shape sent to the provider, against a fake client — no network).

## Module boundaries

| Module | Responsibility | Depends on | Used by |
|---|---|---|---|
| app/config.py | settings, clock | stdlib | everything |
| app/schemas.py | all data contracts | pydantic | everything |
| app/state.py | persistence and seed | config | tools, policy, agent, main |
| app/tools.py | mocked hospital + faults | schemas, state | policy only |
| app/policy.py | the gate: authorize/execute/verify | tools, state, schemas | agent |
| app/retrieval.py | approved-content retrieval | yaml | agent |
| app/prompts.py | instructions and code-composed templates | tools (fmt_time) | agent |
| app/llm.py | LLM protocol, scripted fake, OpenAI adapter | schemas, tools, config | agent |
| app/agent.py | turn loop and output guard | all above | main |
| app/logging_.py | JSON logging with redaction | stdlib | auth, agent, main |
| app/auth.py | bearer → patient | logging_ | main |

`policy.py` is the only module allowed to call `tools.py` for writes; `agent.py` never touches the store's hospital tables directly.

## Code quality practices

TDD per task against the reviewed implementation plan (in `docs/superpowers/plans/`); ruff (line length 100, py312); exactly five runtime dependencies; stdlib for SQLite, hashing, HMAC, concurrency; deterministic clock and seeded data everywhere; fault injection as a first-class test tool; inline env comments stripped so a copied `.env.example` cannot silently break configuration.

## What I intentionally did not build in 48 hours

- **Embeddings / vector store** — no measured gain at ~40 chunks; switch points documented in ADR-002. Build when KB size or recall demands it.
- **Real identity provider** — mock table stands in; OIDC integration is the first pre-production action (README).
- **Postgres / Redis** — SQLite is deliberate for the pilot (ADR-004); Postgres arrives with the production topology.
- **Rate-limiting middleware, gateway** — belongs at the ingress in production, not inside the service.
- **Streaming, UI** — not needed to prove the safety architecture; latency is bounded and measured.
- **Full Arabic KB and native validation** — the pipeline is proven with three bilingual docs and two Arabic eval cases; completion is a pre-production action.
- **PII-detection models, async queues, metrics stack, multi-tenancy, CI pipeline** — documented in `docs/06`; a CI pipeline running `ruff + pytest + fake evals` is a one-file addition and the first thing I would add after submission.

## Known weaknesses in the code

- Tool-call timeout uses a throwaway thread per call (`tools.call`); a production adapter calls the real API with a client-side timeout instead.
- SQLite single writer, and the shared connection means concurrent requests can interleave transactions — safe for the sequential demo and tests, a stated pilot limitation; production uses Postgres with per-request connections/pool.
- Pending-action expiry is checked at use, not swept by a job; expired pendings linger as rows until replaced.
- Department names inside Arabic templates remain in English (`docs/assumptions.md`).
