# Production readiness (Part 6)

## 1. Load model

100,000 interactions per month is about 3,300 per day. With clinic-hours concentration (Sun–Thu, 08:00–17:00, 9 hours) that is about 6 per minute on average and roughly 20 per minute at peak (3× factor). A turn is 3–8 seconds of mostly network wait, so even one replica handles the throughput; run 3 replicas across zones for availability, not scale. The design target is simplicity a two-person team can operate.

## 2. Topology

API gateway (TLS, auth offload, rate limits) → stateless assistant service (this code) → managed Postgres (conversations, pending actions, ledger, audit) → hospital scheduling API behind the tool adapter → model provider. A managed queue (SQS/RabbitMQ-class) is used only for asynchronous work: nightly reconciliation, notifications, audit export. The request path stays synchronous on purpose — a claimed success must be backed by a verified read inside the same request.

Pilot-to-production change worth naming: the submission's SQLite connection is shared and single-writer; under concurrent requests, interleaved transactions can error. Production Postgres with a connection pool removes this class of issue entirely, which is one reason the state move is prerequisite, not optional.

## 3. Rate limits

Per patient: 20 messages/minute and 200/day at the gateway. Per service: a concurrency cap on model calls (e.g. 32) with 429 back-pressure. Tool adapter calls capped by the hospital's contract; escalate to the hospital team before raising.

## 4. Retries and timeouts

Model: one retry with backoff (in code), 30 s timeout. Tool reads: one retry. Tool writes: no automatic retry — the client may resend the confirmation because execution is idempotent by token. Circuit breaker: after 5 consecutive provider failures, return `MODEL_UNAVAILABLE` immediately for 60 s.

## 5. Observability

JSON logs and audit rows shipped to a central store; dashboards for the metrics in `docs/04`; alerts as listed there; trace id = `request_id` across logs and audit. Weekly review includes a 50-conversation human QA sample.

## 6. Model and provider fallback

A second model (`gpt-5.6-terra`) configured behind the same golden-set gate. Provider switch is by configuration and deployment, not runtime — a fallback model must have passed the evals first; silently routing patients to an unevaluated model is a safety regression, not resilience.

## 7. Deployment and rollback

Immutable container image (the provided Dockerfile is the template); configuration only from environment; versioned prompt (hash of `app/prompts.py` logged per turn) and versioned KB (manifest version). Rolling deployment with health checks (`/healthz`). Rollback = previous image plus previous KB manifest, both retained 30 days; database changes backward compatible for one release so the old image runs against the new schema.

## 8. Incident handling

Severities: **S1** — any unverified action reported as success, or any PHI exposure; **S2** — service down or model provider down; **S3** — quality regression. On-call rotation; runbook in `docs/08`; patient communication owned by Patient Services, not by engineering.

## 9. Cost controls

Budget alerts at 50/80/100% of the monthly model budget; per-turn caps (input 8,000 tokens, 4 iterations, 6 tool calls); prompt-prefix caching; history truncated to the last 20 messages; weekly cost-per-interaction review; a kill switch that switches the gate to read-only mode (writes denied, reads and escalations continue).

## 10. Operating this at Apex

Two people: one on-call engineer, one Patient Services owner for escalations and KB content. A weekly 30-minute review of the metrics dashboard and the conversation sample. Everything above is sized so that operating the system is a small fraction of one person's week; anything that breaks that bar is a design bug.
