# Patient-Service Assistant (Apex AI Arabia, Senior AI Engineer assessment)

## What I Would Change Before Production

Ordered by priority. Each item names the gap in this submission and the action required before patient traffic.

1. **Identity.** Replace the mock bearer-token table (`app/auth.py`, `app/state.py`) with the hospital identity provider (OIDC), bind conversations to the verified patient, and add per-patient rate limits at the gateway.
2. **Hospital integration and independent verification.** Put the real scheduling system behind the tool adapter (`app/tools.py`) with an independent read path for the read-back check in `app/policy.py`, compare a record version or etag, and add a nightly reconciliation job that flags any ledger entry the system of record does not reflect.
3. **Data residency and legal validation.** Before any patient message leaves the hospital network, obtain qualified validation of PDPL and health-sector obligations for external model processing, and choose one of: OpenAI with a zero-data-retention agreement, Azure OpenAI in an approved region, or a self-hosted model in-Kingdom. `store=False` is set today; it is not a compliance answer by itself.
4. **Storage, secrets, encryption.** Move state from SQLite to managed Postgres with encryption at rest, keep `OPENAI_API_KEY` and `LOG_HMAC_KEY` in a secret manager with rotation, and enforce TLS end to end.
5. **Knowledge base governance.** Add an owner sign-off and versioned publishing workflow for `kb/`, run the retrieval evals on every KB release, and move to embeddings only when recall on the golden set requires it.
6. **Arabic quality.** Complete the bilingual KB, build an Arabic golden set of at least 30 cases, and have native speakers review the templates in `app/prompts.py`.
7. **Observability and alerting.** Ship JSON logs and the audit table to a central store; alert on any `ACTION_UNVERIFIED`, on claim-mismatch rate, tool error rate, p95 latency, and cost per interaction.
8. **Staged rollout with gates.** Shadow mode, then 5%, 25%, 100% of traffic, each gate requiring golden-set pass rate of at least 95% and zero unverified actions in the previous stage; a staffed queue with an SLA for escalations.
9. **Model governance.** Pin model versions, rerun the golden set on every model or prompt change, and keep a second model configured as fallback behind the same eval gate.
10. **Operations.** Adopt the incident runbook (`docs/08-incident-investigation.md`), define severities, rehearse rollback (`docs/06-production-readiness.md`), and set cost budget alerts.

## What this is

A FastAPI service with one endpoint, `POST /assistant/message`, that helps a patient book, reschedule and cancel appointments, check availability, and get approved information. The model proposes tool calls; a single policy gate (`app/policy.py`) authorizes them, turns appointment writes into confirmation-gated pending actions, executes with idempotency, and verifies every write by reading the record back. The final message is checked against the verified ledger before it is returned.

Design spec: `docs/superpowers/specs/2026-09-22-apex-patient-assistant-design.md`. Implementation plan: `docs/superpowers/plans/2026-09-22-apex-patient-assistant.md` (with recorded pre-execution amendments).

## Quick start

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), or Docker.

```bash
cp .env.example .env            # add OPENAI_API_KEY
uv sync
uv run pytest                   # unit, contract, flow, and fake-mode eval tests
uv run --env-file .env uvicorn app.main:create_app --factory --port 8000
```

Docker:

```bash
docker compose up --build
```

Mock patients: `token-p1001` (patient P-1001, appointments A-1001-1 cardiology and A-1001-2 dermatology) and `token-p1002`. Set `APP_FAKE_NOW=2026-10-05T09:00:00+03:00` for the deterministic demo calendar used by the tests and evals. Note the seeded policy: each patient may hold one active appointment per department (enforced in `app/tools.py`), so the booking demo uses dental — P-1001 already has cardiology.

## Try a two-turn booking

```bash
curl -s -X POST http://localhost:8000/assistant/message \
  -H "Authorization: Bearer token-p1001" -H "Content-Type: application/json" \
  -d '{"message":"Book me a dental appointment on 13 October in the morning"}'
```

The response has `state: AWAITING_CONFIRMATION`, a `pending_confirmation`, and a message ending with a code-composed confirmation block. Send `{"message":"yes","conversation_id":"<id from the first response>"}` to execute. The second response has an action with `status: executed_verified` and a message that starts with "Confirmed:". Nothing is written until that second turn.

Simulate the incident from Part 8 with the header `X-Mock-Fault: reschedule_silent_noop` on both turns of a reschedule: the hospital reports success without changing the record, the read-back fails, the response carries `failure.code: ACTION_UNVERIFIED`, and an escalation is created. Other faults: `malformed_result`, `tool_timeout`, `tool_error`, `slots_empty`. Faults are ignored when `APP_ENV=production`.

## API

`POST /assistant/message` with `Authorization: Bearer <token>`; body `{"message": "...", "conversation_id": "<optional>", "locale": "en|ar|null"}`. Optional headers `X-Request-Id`, `X-Mock-Fault`. Response fields: `conversation_id, request_id, message, state, intents, actions[], sources[], pending_confirmation, escalation_id, failure`. Errors: 401 bad token, 403 conversation belongs to another patient, 404 unknown conversation id, 422 invalid body. `GET /healthz`.

Failure codes: `MODEL_UNAVAILABLE`, `MODEL_OUTPUT_INVALID`, `TOOL_UNAVAILABLE`, `TOOL_MALFORMED`, `ACTION_UNVERIFIED`, `BUDGET_EXCEEDED`, `POLICY_DENIED`.

## Evaluation

```bash
uv run python -m evals.run --mode fake                 # scripted model, deterministic, used in CI
uv run --env-file .env python -m evals.run --mode openai   # real model, writes an evidence report
```

Cases live in `evals/cases.yaml` (30 cases: happy paths, ambiguity, multi-intent, unsafe requests including prompt injection in the message and in retrieved content, cross-patient attempts, the department-limit rule, retrieval failures, tool errors, escalation, confirmation discipline, Arabic, model failures). Reports in `evals/reports/`. See `docs/04-evaluation.md`.

## Deliverables index

| Brief part | Where |
|---|---|
| Final requirement: What I Would Change Before Production | This README, first section |
| Part 1 Architecture and ADRs | `docs/01-architecture-and-adr.md` |
| Part 2 Working implementation | `app/`, `kb/`, `tests/`, this README |
| Part 3 Reliability, safety, security | `docs/03-reliability-safety-security.md` |
| Part 4 Evaluation | `docs/04-evaluation.md`, `evals/` |
| Part 5 Testing and code quality | `docs/05-testing-and-code-quality.md` |
| Part 6 Production readiness | `docs/06-production-readiness.md` |
| Part 7 Cost, latency, model choice | `docs/07-cost-latency-model-choice.md` |
| Part 8 Incident investigation | `docs/08-incident-investigation.md` |
| Part 9 Leadership review | `docs/09-leadership-review.md` |
| Part 10 Client discovery | `docs/10-client-discovery.md` |
| Bonus judgment challenge | `docs/11-bonus-judgment-challenge.md` |
| Assumptions | `docs/assumptions.md` |
| AI tools disclosure | `docs/ai-tools-disclosure.md` |

## Repository layout

```
app/        config, schemas, state (SQLite), tools (mock hospital), policy (the gate), llm, prompts, retrieval, agent, logging_, auth, main
kb/         manifest.yaml plus approved markdown documents (fictional placeholders)
evals/      cases.yaml, run.py, reports/
tests/      pytest suite; fixtures/poisoned-kb for injection tests
docs/       deliverable documents and the design spec and plan
```
