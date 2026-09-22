# Incident investigation (Part 8)

**The incident.** A patient was told a reschedule succeeded; the hospital system shows no change.

## 1. Investigation, in order, with the exact query for each step

1. **Anchor the conversation.** From the patient's report, get the conversation id or an approximate time. All logs and audit rows join on `request_id`; each turn records `turn_started`/`turn_completed` with `conversation_id`.
   `SELECT request_id, event, detail_json FROM audit_events WHERE conversation_id=? ORDER BY id;`
2. **Read the tool trail.** Look for the proposal (`reschedule_appointment`, decision `PENDING`), then the executor: a `confirm_pending_action` row with `status` (`executed_verified` or `failed`), `verified` (true/false), and the `idempotency_key`. The `turn_completed` row shows the final state and failure code the patient's client received.
3. **Read the ledger.**
   `SELECT * FROM action_ledger WHERE idempotency_key=?;`
   `result_json` is what the tool adapter returned; `verified` is what the read-back concluded. A row with `verified=0` and a success-shaped result is the signature of this incident class.
4. **Compare with the system of record now.** In the pilot, the `appointments` table (in production, the scheduling API): the appointment's `start_time`, `status`, and `updated_at` or version versus the ledger's claim.
5. **Check `claim_mismatch` events.** Did the model claim an action the ledger did not have, and did the guard replace the text? If a success message still reached the patient, either the guard was bypassed (bug) or the template itself asserted success without a verified flag (prompt/template bug).
6. **Read the JSON logs by `request_id`** for latency, retries, failure codes, and the prompt/KB versions in effect.

## 2. Likely failure modes

- The tool adapter reported success but the downstream commit failed after acceptance (async settlement), or succeeded on a replica that later lost the write.
- The verification read hit a stale replica/cache — or, as in this pilot, the same faulty adapter as the write (the known shared-path limitation).
- The adapter mapped a missing status field to a default "success" (malformed response accepted as valid).
- An idempotent replay returned a cached success after a downstream rollback.
- The model claimed success with no tool call or after a failed one, and the claim guard or template did not catch it.
- Appointment-id mapping errors between the assistant's ids and the hospital's ids.
- A client retry after a timeout executed twice downstream because the downstream write is not idempotent (ours is, by token; the hospital's must be too — a discovery question).

## 3. Immediate containment

1. Switch the service to read-only mode: writes denied at the gate; reads and escalation continue (the kill switch in `docs/06`).
2. Pull every ledger entry since the last known-good deployment and reconcile against the scheduling system; the ledger exists precisely so this query is minutes, not archaeology.
3. Patient Services contacts affected patients with the correct appointment details — engineering does not message patients.
4. Freeze model and prompt versions and the KB manifest; preserve logs and audit rows (legal hold if applicable).

## 4. Permanent corrective actions

- Tool contracts must return a system-of-record version and a `committed` flag; verification compares versions through an **independent read path**, because a compromised adapter that shares the read path can fake both sides.
- Nightly reconciliation job comparing the ledger to the system of record, alerting on any drift.
- Alert on any `ACTION_UNVERIFIED` (page) and on claim-mismatch rate (ticket).
- Fault-injection contract tests in CI: `reschedule_silent_noop`, `malformed_result`, `tool_timeout` — all three exist in this submission's suite and evals.
- Quarterly chaos rehearsal of this exact scenario; post-incident review with owners and dates.

## 5. How this submission already handles it

Walk-through: `tests/test_agent_flows.py::test_silent_noop_reschedule_is_reported_unverified` and eval `tool_reschedule_silent_noop`. The fault makes the tool return a success-shaped result without changing the record — exactly the incident. The read-back (`policy._verify`) fails, the outcome becomes `ACTION_UNVERIFIED`, an escalation is created automatically, the conversation enters ESCALATED, and the patient message is the code-composed "treat it as not done" template. The model's own "rescheduled successfully!" text is stripped by the guard. In this system, the incident as described cannot be produced by the model's claim alone; it would require the verification read to lie, which is why the independent read path is corrective action #1.

## 6. Surprise scenario: a compromised or malformed tool response

**Diagnosis.** Same audit path as above. A malformed response is rejected at the result schema (`TOOL_MALFORMED`, `tests/test_tools.py::test_fault_malformed`) and never shown as success. A plausible-but-false response is caught by the read-back (`ACTION_UNVERIFIED`, path above). The dangerous case is a compromised adapter that fakes both the write result and the read-back — possible today because both share the adapter.

**What I would change first.** Give the tool result a system-of-record version and verify through an independent path (reporting replica or FHIR feed), comparing versions, not just states. Second: alert and auto-freeze writes after the first unverified action in a window, converting a class of incidents into a bounded outage.
