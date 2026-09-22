# Leadership review: the five biggest risks or shortcuts in this submission

> **OWNER: WRITE THIS DOCUMENT YOURSELF.** As the senior engineer responsible for approving this for
> production, the review must be in your words and defensible live. The scaffold and the candidate
> list below are prompts, not content. Delete this notice when done.

As the senior engineer responsible for approving this for production, I would not release it today. The five items below are ordered by the harm they could cause.

## 1. <risk or shortcut>
What it is: <two sentences>
Why it matters: <one sentence, in terms of patient or business impact>
What I require before release: <specific, testable requirement>

## 2. ...
## 3. ...
## 4. ...
## 5. ...

## What I would sign off on today
<the narrow scope, if any, that could go to a controlled pilot as is>

---

Candidate risks the owner should consider ranking (from the implementation and its documentation —
verify each against the code before claiming it): verification reads through the same adapter it
verifies (`docs/08` §5); mock identity (README item 1); SQLite single-writer and the shared
connection under concurrency (`docs/05`); Arabic not validated by native speakers; retrieval
empty-answer discipline is prompt-governed, not code-enforced; pending expiry not swept; no rate
limiting; fictional KB content; prices valid only as of 2026-09-22; a single real-model eval run
rather than repeated runs.
