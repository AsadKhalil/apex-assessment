# Assumptions

1. Identity: a mock bearer-token table stands in for the hospital identity provider. Tokens `token-p1001` and `token-p1002` are test fixtures.
2. Single tenant, single hospital group, four departments. Slot and appointment data are seeded fixtures under a fixed demo clock (2026-10-05T09:00:00+03:00, a Monday).
3. Timezone Asia/Riyadh (UTC+3, no DST); work week Sunday to Thursday.
4. One active appointment per department per patient is hospital policy and is enforced in `app/tools.py` (`DepartmentLimit`); the booking demo and evals therefore use dental for P-1001, who already holds a cardiology appointment.
5. Language: English is the evaluated language; Arabic is supported by the pipeline (templates, tokenizer, three bilingual KB sections, two Arabic eval cases) but not validated by native speakers. Pending-action summaries and department names inside Arabic templates are English.
6. Knowledge base: every document in `kb/` is a fictional placeholder written for this assessment, including the phone number, locations, hours, and policies. The KB names the Saudi Insurance Authority as the health-insurance regulator (the former CCHI's mandate moved to the IA in March 2024); the client replaces all content through the manifest.
7. Emergency guidance uses 997 as the ambulance number; the client must validate the number and wording for each region.
8. Insurance: the assistant gives general information only and never determines coverage, cost, or eligibility; the Insurance Desk owns those answers.
9. Regulatory: applicability of the Saudi Personal Data Protection Law and health-sector rules to external model processing has not been assessed here and needs qualified validation before production. `store=False` is set on every model call as a technical measure, not as a compliance conclusion.
10. Model prices in `docs/07` were read from OpenAI's pricing page on 2026-09-22 and must be re-verified before any budget decision.
11. Verification reads through the same mock adapter it verifies; production requires an independent read path (`docs/08`).
12. Pending actions expire after 10 minutes and are not swept by a background job; expiry is checked on use.
13. No rate limiting, streaming, UI, request-level idempotency for retried POSTs, or multi-tenancy in this submission.
