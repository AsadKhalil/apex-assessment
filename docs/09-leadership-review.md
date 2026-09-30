# Leadership review: the five biggest risks or shortcuts in this submission

As the senior engineer responsible for approving this for production, I would not release it today. The five items below are ordered by the harm they could cause. Each names where the gap lives in the code and what I would require — specific and testable — before patient traffic.

## 1. Write verification shares its path with the write itself

What it is: after an executed write, `_verify()` in `app/policy.py` reads the patient's appointments through the same tool adapter that performed the write. The system's core promise — "success is only reported after read-back" — is only as trustworthy as that shared path. A compromised or buggy adapter can fake both sides; the fault-injected no-op (`reschedule_silent_noop`) is caught only because the mock's read path stays honest.

Why it matters: this is the exact class of failure the Part 8 incident describes. Everything else in the submission narrows that risk; this residual hole reopens it under a malicious or badly integrated real system.

What I require before release: the tool contract returns a system-of-record version or etag on every write; the verification read goes through an independent path (reporting replica or FHIR feed) and compares versions, not just state; a nightly reconciliation job compares the action ledger against the system of record and alerts on drift (`docs/08` §4). All three are acceptance criteria for the integration milestone, not backlog items.

## 2. Identity is a mock bearer-token table

What it is: `app/auth.py` resolves any holder of `token-p1001` to patient P-1001 via a seeded SQLite table. There is no real authentication, no token expiry, no revocation, and no per-patient rate limiting.

Why it matters: in production this is not an authentication system; it is the absence of one. Cross-patient protection currently rests on a fixture.

What I require before release: the hospital identity provider (OIDC) issues the patient identity; conversations bind to the verified subject claim; tokens expire; per-patient rate limits (20/minute, 200/day per `docs/06`) are enforced at the gateway; an integration test proves a revoked or mismatched identity cannot read or act on a conversation (403/401 paths already exist in `tests/test_api.py`).

## 3. SQLite state: single writer, one shared connection

What it is: all state — conversations, pendings, ledger, audit — lives in one SQLite database accessed through a single connection shared across request threads (`app/state.py`, documented in `docs/05`/`docs/06`). Correct for the sequential pilot and fully exercised by 106 tests; not correct under concurrent traffic, where interleaved transactions can error.

Why it matters: at the stated volume (about 20 messages/minute peak) the load is trivial, but a booking surge could produce intermittent 500s — an availability incident that would be entirely self-inflicted.

What I require before release: managed Postgres with a connection pool and the same repository interfaces; a load test at 3× the modeled peak with zero transaction errors and p95 under the 8 s alert line; the migration rehearsed against a production-shaped database.

## 4. Arabic is plumbed but unvalidated

What it is: the pipeline handles Arabic end to end — tokenizer, bilingual templates, three bilingual KB documents, two Arabic eval cases — but no native speaker has reviewed any of it, pending-action summaries render in English inside Arabic messages (a visible wart in the demo), and there is no Arabic golden set. `docs/assumptions.md` records this honestly.

Why it matters: for a patient-facing service in the Kingdom, an unnatural or wrong Arabic sentence is not a cosmetic issue; it is a trust and clinical-safety issue, and it will be the first thing a Saudi client notices.

What I require before release: native-speaker review of every template and KB document; an Arabic golden set of at least 30 cases passing at the same 95% gate as English; department names localized in `verified_success()`.

## 5. Real-model evidence is one good run

What it is: the committed evidence is a single 28/28 run on `gpt-5.6-luna` (`evals/reports/2026-09-28-openai-gpt-5.6-luna.md`). Getting there took four runs whose failures were real: proposal timing varied run-to-run, refusals tripped wording checks, and the model once hallucinated a slot id. The final prompts and expectations are calibrated to one run's behavior, and two guardrails remain prompt-governed rather than code-enforced (the model declining to answer from memory when retrieval is empty; consent interpretation).

Why it matters: a 100% single run can overfit; variance across runs is the number that predicts production quality, and I have not measured it.

What I require before release: five consecutive full-suite runs with no failure in the `unsafe`, `tool_error`, or `confirmation` categories before any rollout gate; the golden set runs in CI on every prompt or model change; the empty-retrieval discipline gains a code-level check (a structured `sources_empty` answer path) rather than relying on instructions.

## What I would sign off on today

A controlled internal pilot, English-only, with the write flows in shadow mode — proposals generated and audited but executed by a human using the same ledger — plus live read-only features (availability, preparation, insurance general information) and escalations, behind the hospital's real identity provider, with Patient Services staffed for every escalated conversation. That scope exercises the genuinely production-grade parts of this system (the gate, the ledger, the audit trail) without exposing a patient to the five gaps above.
