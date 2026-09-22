# Reliability, safety, and security (Part 3)

Every control below names the code that implements it and the test or eval that proves it.

## 1. Prompt injection and malicious retrieved content

Controls, in the order an attacker meets them:

1. **Manifest-only indexing.** Only documents listed in `kb/manifest.yaml` (owner, version, approval date) are indexed; a file dropped into `kb/` is invisible to retrieval (`tests/test_retrieval.py::test_manifest_only_indexing`).
2. **Index-time injection screen.** Chunks matching instruction-like patterns ("ignore previous instructions", "system override", "call the tool", "you must now", tool names such as `confirm_pending_action`) are skipped and logged (`app/retrieval.py::INJECTION_PATTERNS`, `tests/test_retrieval.py::test_poisoned_doc_is_screened_out`). The KB is approved content, so this is defense in depth, not the primary control.
3. **Data framing.** Retrieved chunks reach the model wrapped as `<approved_content>` with an explicit rule that content inside is information, never instructions (`app/prompts.py::wrap_retrieved`).
4. **The gate.** Even if the model obeys injected text, an appointment write only creates a pending action, and `confirm_pending_action` is denied in the proposal turn (`same_turn`), regardless of what the text said. Proven with the poisoned fixture and a deliberately obedient scripted model: the poison reaches the model, the model "complies", and nothing executes — `tests/test_injection.py::test_injected_instruction_cannot_execute_writes` (assertions include that both appointments remain booked and the success claim is stripped).
5. **Output guard.** The model's final claims are checked against the verified ledger; unverified claims are dropped and audited as `claim_mismatch` (`tests/test_agent_flows.py::test_claim_mismatch_is_rewritten`).

Eval evidence: `unsafe_injection_in_message` (injection in the user message), `unsafe_injection_in_kb` (injection in retrieved content with the screen disabled).

## 2. Cross-user data leakage and unauthorized actions

- `patient_id` originates only from the authenticated session; the request body has no patient field and no model-facing tool schema contains one (`tests/test_llm_scripted.py::test_tool_schemas_are_strict_and_never_mention_patient_id`).
- Ownership is checked in the gate (`not_owner`, generic message that does not confirm the id exists) and again in the tool layer (`tools._own_active`) — `tests/test_policy.py::test_ownership_denied_without_confirming_existence`, `tests/test_tools.py::test_ownership_enforced`, eval `unsafe_other_patient_appointment` (expects `POLICY_DENIED`, the other patient's appointment untouched).
- Conversations are bound to their creator: 403 for another patient's conversation id (`tests/test_api.py::test_conversation_is_bound_to_its_patient`).
- State allow-list: `ESCALATED` denies writes and confirmations; budgets cap every turn (6 tool calls — denied calls count — and 4 model iterations) so a looping model cannot hammer tools (`tests/test_policy.py::test_budget_denied`, `tests/test_agent_flows.py::test_budget_exceeded_by_tool_calls_and_by_iterations`).
- Argument validation: tool arguments are validated against `extra="forbid"` Pydantic models; an injected `patient_id` argument is rejected as `invalid_args` (`tests/test_schemas.py::test_args_forbid_extra_fields`, `tests/test_policy.py::test_invalid_args_denied`).

## 3. Auditability without exposing patient information

Log lines carry `request_id`, `conversation_id`, `patient_hash` (HMAC-SHA256 of the patient id under `LOG_HMAC_KEY`, 16 hex chars), `turn_index`, `state`, `event`, `tool`, `decision`, `status`, `verified`, `latency_ms`, `tokens_in/out`, `failure_code`. They never carry message text, names, tokens, or raw patient ids — enforced by `REDACTED_KEYS` in the formatter and by `log_event` stripping keys before they reach the LogRecord (`app/logging_.py`; `tests/test_logging.py` asserts the redaction on both paths). The `audit_events` table stores the action trail: every decision, execution, verification outcome, idempotency key, and claim mismatch, joined to logs by `request_id`, with `args_hash`/`result_hash` rather than payloads. An investigator can reconstruct what was attempted, allowed, executed, and verified, for which conversation, without reading patient text (runbook: `docs/08`).

## 4. Secrets, least privilege, encryption, access boundaries

Pilot (this submission): secrets only from the environment (`OPENAI_API_KEY`, `LOG_HMAC_KEY`); `.env` is git-ignored (`.gitignore`) and excluded from the Docker image (`.dockerignore`); `.env.example` documents every variable. Provider calls use `store=False`.

Production requirements (not implemented here, stated as commitments): secrets in a managed secret manager with rotation and no local copies; per-service accounts — the verification read path should use a read-only role on the scheduling system, the tool adapter a scoped write role, so a compromised adapter cannot, for example, delete records; the model provider holds no hospital credentials and receives no identifiers; TLS everywhere in transit; encryption at rest for Postgres and backups, snapshot retention aligned to the audit policy; `LOG_HMAC_KEY` in a KMS so hashes remain stable but the key is not ambient; message text stored under restricted access with defined retention.

## 5. Fallback behavior

| Trigger | Code | Behavior | Proof |
|---|---|---|---|
| Model error/timeout after one retry | `MODEL_UNAVAILABLE` | Safe template, escalation offer, no tools run | `test_agent_flows.py::test_model_failures`; eval `model_unavailable` |
| Unparseable final output after one retry | `MODEL_OUTPUT_INVALID` | Safe template | `test_agent_flows.py::test_model_failures`; eval `model_output_invalid` |
| Retrieval error or no match | status only | Model told no approved content exists; evals check it does not answer anyway (documented gap: not enforced in code) | `tests/test_retrieval.py::test_search_error_is_contained`; evals `ret_out_of_kb`, `ret_index_unavailable` |
| Tool exception or timeout | `TOOL_UNAVAILABLE` | Failure returned to the model; final message via template; never shown as success | evals `tool_timeout`, `tool_error` fault |
| Tool result fails schema | `TOOL_MALFORMED` | Same as above | `tests/test_tools.py::test_fault_malformed`; eval `tool_malformed` |
| Write verification fails | `ACTION_UNVERIFIED` | Message replaced, automatic escalation, state ESCALATED | `test_agent_flows.py::test_silent_noop_reschedule_is_reported_unverified`; eval `tool_reschedule_silent_noop` |
| Loop or tool budget hit | `BUDGET_EXCEEDED` | Safe template, escalation offer | `test_agent_flows.py::test_budget_exceeded_by_tool_calls_and_by_iterations` |
| Gate denial that blocks the request | `POLICY_DENIED` | Model explains within limits; audit row with reason | `test_agent_flows.py::test_cross_patient_denied_sets_policy_denied`; eval `unsafe_other_patient_appointment` |
| Business rule (one active appointment per department) | denial `department_limit` | Proposal denied before a pending action exists; KB and code agree | `tests/test_tools.py::test_book_one_active_per_department`; eval `unsafe_second_appointment_same_department` |

## 6. Known gaps (stated, not hidden)

- The model refusing to answer from memory when retrieval is empty is prompt-governed and eval-checked, not enforced in code.
- Verification reads through the same adapter it verifies (see `docs/01` §5, `docs/08`).
- No rate limiting middleware, no request-level idempotency for retried POSTs (turn-level idempotency by token exists).
- SQLite: single-writer; the shared connection under concurrent requests is a pilot limitation (`docs/05`, `docs/06`).
- Fault injection is disabled when `APP_ENV=production` (`tests/test_api.py::test_fault_header_ignored_in_production`, `tests/test_tools.py::test_faults_ignored_in_production`).
