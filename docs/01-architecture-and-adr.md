# Architecture and decision record (Part 1)

One FastAPI service. `POST /assistant/message` runs a bounded tool loop: the model receives the system instructions, tool schemas, retrieved approved content, conversation history, and the user message, and returns either function calls or a final structured answer. Every function call passes through `policy.authorize()` — the single gate that decides ALLOW, DENY, or PENDING. Appointment writes become pending actions that require an explicit confirmation in a later turn. After any executed write, the gate reads the record back and verifies the post-state before anything is reported as success. A final output guard (`agent.compose()`) checks the model's claimed actions against the verified ledger and replaces the message with a code-built template on mismatch.

```
client --POST /assistant/message--> main.py (auth, request_id, schema)
                                        |
                                        v
                                   agent.run_turn
          retrieval.search <------------+
                                        +--> llm.respond (OpenAI Responses API | ScriptedLLM)
                                        |        | function_call items
                                        |        v
                                        |   policy.authorize --> policy.execute --> tools.* (SQLite mock hospital)
                                        |        |                    | read-back verification
                                        |        v                    v
                                        |   function_call_output   state.ledger / audit
                                        |
                                        +--> output_guard(final, ledger)
                                        v
                                   AssistantResponse (message, state, actions, sources, failure)
```

## 1. Model

Default `gpt-5.6-luna` via the OpenAI Responses API (`app/llm.py::OpenAIResponsesClient`), with strict function tools and a strict final schema (`AssistantOutput` via `responses.parse(..., text_format=...)`). Every call passes `store=False` and replays prior output items (including encrypted reasoning items, requested with `include=["reasoning.encrypted_content"]`) client-side, so the provider holds no conversation state. Compared alternative: `gpt-5.6-terra` — numbers and the evidence-based default in `docs/07`. The model is switchable by environment variable (`OPENAI_MODEL`); tests and CI run against `ScriptedLLM`, so no provider call is needed to prove the control logic.

## 2. API and backend

`app/main.py` exposes a factory (`uvicorn app.main:create_app --factory`) with `POST /assistant/message` and `GET /healthz`. `app/auth.py` maps a bearer token to a patient (mock identity table; production replaces this with the hospital IdP). Requests carry an optional `X-Request-Id`, propagated to logs and audit rows. Conversations are bound to the patient who created them: another patient's token gets 403, an unknown id gets 404 (`tests/test_api.py::test_conversation_is_bound_to_its_patient`). The response contract is the Pydantic model `AssistantResponse` (`app/schemas.py`), contract-tested in `tests/test_schemas.py` and `tests/test_api.py`.

## 3. Retrieval (RAG)

`app/retrieval.py`: only files listed in `kb/manifest.yaml` (with owner, version, approval date) are indexed; chunking is by `##` heading capped at ~250 words; ranking is BM25 (k1=1.5, b=0.75) with a Unicode tokenizer that handles Arabic; chunks below a score threshold return `no_match`. Retrieved chunks are wrapped as data (`<approved_content source="...">`) with an explicit rule that content inside is information, never instructions. Why lexical and not embeddings: see ADR-002. An index-time injection screen skips instruction-like chunks and logs them (defense in depth on top of the gate).

## 4. Tool execution

`app/tools.py` implements the six mocked hospital tools from the brief (availability, book, reschedule, cancel, my appointments, escalation) over SQLite, with result schemas validated on return, ownership checks in the tool layer, slot consumption/restoration inside transactions, a one-active-appointment-per-department rule, and fault injection (`X-Mock-Fault`, non-production only).

**A deliberate seventh tool.** The brief lists six mocked tools; this system adds `confirm_pending_action` as an internal executor. Rationale: the two-turn confirmation must be enforceable in code, not in prompts. A write proposal returns a pending action with a server-held token; executing the write requires a later-turn call carrying that token, and the gate (not the model) decides whether the conditions hold. The model cannot bypass the confirmation step because there is no path from "propose" to "executed" inside one turn — `tests/test_policy.py::test_confirm_never_in_same_turn` and `tests/test_injection.py` prove it.

`app/policy.py` is the only module allowed to execute writes:

- `authorize()` order: unknown tool → budget (max 6 tool calls per turn; denied calls count too) → argument validation against the tool's Pydantic model (`extra="forbid"`; `patient_id` is not in any model-facing schema) → state allow-list (ESCALATED blocks writes and confirms) → confirmation-token rules (exists, matches, unexpired, and `turn_created < turn_index` — never the proposal turn) → ownership (any `appointment_id` must belong to the authenticated patient; the denial is generic and does not confirm the id exists) → reads ALLOW, appointment writes PENDING.
- `execute()` runs the tool with a timeout, validates the result model (malformed → `TOOL_MALFORMED`), and for confirmed writes: idempotency key = sha256(conversation, tool, args_hash, token) — a replayed confirmation returns the stored result instead of re-executing (`tests/test_policy_execute.py::test_confirm_executes_verifies_and_is_idempotent`); then read-back verification (below); then audit.

## 5. Read-back verification

`policy._verify()` calls `get_patient_appointments` and checks the post-condition of each write type (book: appointment exists at the slot's time with status booked; reschedule: start time equals the new slot's; cancel: status cancelled). A tool that reports success without changing the record produces `ACTION_UNVERIFIED`, an automatic escalation, state ESCALATED, and a code-composed message that tells the patient nothing was confirmed (`tests/test_agent_flows.py::test_silent_noop_reschedule_is_reported_unverified`, eval case `tool_reschedule_silent_noop`). Success sentences for writes are always composed by code from verified state (`app/prompts.py::verified_success`), never by the model.

Documented limitation: in this pilot the verification read goes through the same mock adapter it verifies. Production requires an independent read path and a system-of-record version — `docs/08` treats this as the first corrective action.

## 6. State

`app/state.py` holds everything in SQLite via stdlib: identity, slots, appointments, escalations, conversations, messages, pending actions (exactly one open per conversation; a new proposal marks the previous one `replaced`), the action ledger (idempotency), and audit events. Conversation states: `IDLE`, `AWAITING_CONFIRMATION` (write proposed), `ESCALATED` (reads and escalation notes only; writes denied). Pending actions expire after 600 seconds, checked at confirmation time. Production moves state to Postgres (ADR-004).

## 7. Authorization

`patient_id` comes only from the authenticated session (`app/auth.py`), never from the request body or the model. Model-facing tool schemas contain no patient identifier (`tests/test_llm_scripted.py::test_tool_schemas_are_strict_and_never_mention_patient_id`). Ownership is enforced twice: in the gate (`authorize()` → `not_owner`) and again in the tool layer (`tools._own_active`), so neither a confused model nor a future code path can act on another patient's appointment. A patient cannot even learn whether an appointment id exists (`tests/test_policy.py::test_ownership_denied_without_confirming_existence`).

## 8. Observability

`app/logging_.py` emits JSON lines with `request_id`, `patient_hash` (HMAC-SHA256, keyed), state, latency, tokens, failure codes, and tool names. Message text, names, tokens, and raw patient ids never reach the logs (`tests/test_logging.py`). The `audit_events` table records `turn_started`, every `tool_call` (decision, status, verified, idempotency key), `claim_mismatch`, and `turn_completed` — the action trail an investigator joins to logs by `request_id` (`docs/08`).

## 9. Data boundaries

What leaves the service: system instructions, retrieved approved content, the conversation's message text, and tool results — to OpenAI only, with `store=False`. What never leaves: bearer tokens, patient identifiers (only the HMAC hash is logged), other patients' data (ownership scoping), secrets. What is stored locally: message text and pending-action args in SQLite (pilot), argument and result hashes in audit, never full payloads. Cross-border processing of patient text needs qualified legal validation before production — stated in `docs/assumptions.md`, not assumed here.

## 10. Scaling and failure isolation

The request path is stateless apart from the SQLite store, so replicas scale horizontally once state moves to Postgres. Every turn is bounded: at most 4 model iterations, 6 tool calls, a 2-second tool timeout (thread-isolated), a 30-second provider timeout, one model retry with backoff. Every failure maps to an explicit code (`MODEL_UNAVAILABLE`, `MODEL_OUTPUT_INVALID`, `TOOL_UNAVAILABLE`, `TOOL_MALFORMED`, `ACTION_UNVERIFIED`, `BUDGET_EXCEEDED`, `POLICY_DENIED`) with a safe, code-composed message — a failed tool can never become a claimed success. Load arithmetic and the production topology are in `docs/06`.

## ADR-001 Orchestration: propose-dispose instead of planner or free agent

**Context.** The assistant must handle free-text, multi-intent messages (favoring model flexibility) while never executing an unconfirmed or unauthorized write (favoring determinism).

**Options.** A) Fully deterministic workflow: the model only extracts intents and phrases text; code plans every step. B) Native agent loop: the model drives tools directly, constrained only by the prompt. C) Model proposes within a per-state allow-list; a single code gate authorizes, a later-turn confirmation executes, code verifies and composes success text.

**Decision.** C.

**Consequences.** A is safest but brittle on phrasing and multi-intent, and needs a custom planner schema per flow. B is fastest to build but cannot be proven safe, and is exactly the "impressive but fragile autonomy" the brief warns against. C keeps the language work in the model and the trust decisions in one readable, unit-testable function; the costs are more state handling (pending actions, tokens, ledger) and a stricter conversation flow, all covered by tests. The gate is tested in isolation over every state and tool combination that matters (`tests/test_policy.py`, `tests/test_policy_execute.py`).

## ADR-002 Retrieval: lexical BM25 over an approved manifest, not embeddings

**Context.** Answers to information questions must come only from approved content; the pilot KB is ~40 English chunks with some Arabic sections; tests and CI must run offline and deterministically.

**Options.** Embeddings + vector store; lexical BM25 over manifest-approved markdown.

**Decision.** BM25. With ~40 chunks the lexical index retrieves the expected top hits (proven in `tests/test_retrieval.py`) with zero external calls on the hot path, no embedder dependency for tests, and full determinism.

**Consequences.** No semantic matching for paraphrases; Arabic retrieval works through shared-script terms but is not validated for dialects. Switch points (recorded, not hypothetical): KB above ~200 chunks, Arabic-against-English queries, or golden-set recall below target — then move chunk embeddings behind the same `Retriever` interface, keeping the manifest and the injection screen.

## ADR-003 Confirmation and verification: two-turn writes with read-back

**Context.** Appointment changes are consequential: a wrong "success" message sends a patient to a hospital visit that does not exist (the Part 8 incident).

**Decision.** Every appointment write is a two-step flow across turns with a server-held token (`turn_created < turn_index` enforced); every executed write is verified by read-back against the patient's appointments; success text is composed by code from the verified ledger; the model's final claims are checked against that ledger and rewritten on mismatch.

**Consequences.** One write at a time per conversation (multi-intent handled sequentially, documented); an extra turn of latency for every write (accepted; correctness over speed); the residual risk that the verification read shares a path with the write adapter — mitigated in production by an independent read path and a version/etag comparison, stated as the first pre-production action in the README and `docs/08`.

## ADR-004 State: SQLite via stdlib for the pilot, Postgres for production

**Decision.** SQLite (WAL) holds all state for the assessment: zero services to operate, transactional writes, deterministic tests against a file or `:memory:`.

**Consequences.** Single writer; the shared connection is safe for the sequential demo and tests but is a known pilot limitation under concurrent requests (see `docs/05`, `docs/06`). Production uses managed Postgres for conversations, pending actions, ledger, and audit, with the hospital system of record behind the tool adapter; Redis only if distributed locks are ever needed.

## ADR-005 Model: cost tier by default, judged by evidence

**Decision.** Default `gpt-5.6-luna` (cost-sensitive high-volume tier), switchable by env var, with `gpt-5.6-terra` as the compared alternative. The default is confirmed or changed by the real-model eval run (`docs/07` records the numbers); a fallback model must pass the same eval gate before it becomes the default.

**Consequences.** If the cost tier fails judgment cases (emergency escalation, insurance boundaries, consent discipline), the default moves to the stronger tier and the report records why. Provider independence is maintained by the `LLMClient` protocol (`app/llm.py`), so a self-hosted model is an adapter change, not an architecture change.
