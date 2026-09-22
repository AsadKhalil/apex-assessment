# Cost, latency, and model choice (Part 7)

## 1. Verified prices

Source: OpenAI API pricing (developers.openai.com/api/docs/pricing), read 2026-09-22; re-verify on the day of any budget decision. GPT-5.6 tiers announced 2026-07-30; Luna was reduced from $1/$6 to $0.20/$1.20 at launch — prices in this class move, which is why the default is env-configurable and the evidence report records the run date.

| Model | Input per 1M | Cached input per 1M | Output per 1M |
|---|---|---|---|
| gpt-5.6-luna | $0.20 | $0.02 | $1.20 |
| gpt-5.6-terra | $2.00 | $0.20 | $12.00 |
| gpt-5.6-sol | $4.00 | not verified | $20.00 |
| gpt-5-mini | $0.25 | $0.025 | $2.00 |
| gpt-5-nano | $0.05 | $0.005 | $0.40 |
| gpt-4.1-mini | $0.40 | $0.10 | $1.60 |

## 2. Token budget per interaction

Assumptions until the measured report replaces them: 1.6 model calls per turn; per call ≈ 3,500 input tokens (2,000 static instructions + tool schemas — cacheable; 600 retrieved content; 600 history; 300 tool results) and ≈ 250 output tokens. Per interaction: 5,600 input (3,200 cacheable), 400 output. The openai-mode eval report (`evals/reports/`) prints measured tokens per turn; docs and decisions use those when present.

## 3. Cost table

Arithmetic shown once: luna, no cache = 5,600 × 0.20/1e6 + 400 × 1.20/1e6 = $0.00112 + $0.00048 = $0.00160.

| Model | Per interaction, no cache | Per interaction, prefix cached | Per month at 100k, no cache | Per month at 100k, cached |
|---|---|---|---|---|
| gpt-5.6-luna | $0.00160 | $0.00102 | $160 | $102 |
| gpt-5.6-terra | $0.01600 | $0.01024 | $1,600 | $1,024 |
| gpt-5.6-sol | $0.03040 | not computed (cached price unverified) | $3,040 | — |
| gpt-5-nano | $0.00044 | $0.00030 | $44 | $30 |

## 4. Main cost drivers

The static prefix (instructions + tool schemas) is the largest block and the most cacheable; model calls per turn (multi-intent and confirmation turns double it); history length (capped at 20 messages); retrieved chunk size (3 chunks × ~250 words). And the honest framing: model cost is small next to the humans — at a 5% escalation rate, 100k interactions mean 5,000 human touches a month, which dominates the bill. That is an argument for investing in containment quality, not for a cheaper model.

## 5. Latency

Levers, in order of effect: fewer model iterations (parallel tool calls, tighter instructions), prompt-prefix caching (also improves time-to-first-token on supported providers), shorter static prefix, streaming the final message (not built). Measured p50/p95 per turn appear in the dated eval report; the loop is sequential by design (each iteration depends on tool results), so iteration count is the latency lever that matters.

## 6. Caching opportunities

Provider prefix caching (automatic on identical prefixes — keep the instructions byte-stable and ahead of variable content); retrieval results keyed by message hash (short TTL); slot availability per department per minute; tool schemas built once at startup.

## 7. When a smaller or local model is preferable

Smaller (`gpt-5-nano`): only if the golden set passes at that tier — run the same eval; it did not run for this submission, so no claim is made. Self-hosted open weights: justified by data residency or offline requirements, or by volume — with a fixed node cost F per month, break-even interactions = F / per-interaction API cost; at ~$0.001 per interaction, a $2,000/month node breaks even at ~2 million interactions, twenty times the stated volume. At 100k/month the choice is compliance-driven, not cost-driven.

## 8. Decision

Default `gpt-5.6-luna`, switchable by `OPENAI_MODEL`. The rule: the default stays on the cost tier if its committed real-model report shows 100% on the `unsafe`, `tool_error`, and `confirmation` categories and ≥ 90% overall; otherwise the default moves to `gpt-5.6-terra` under the same rule, with both reports kept as evidence. The numbers live in the dated report under `evals/reports/`, not in this document, so they can never drift from the run that produced them.
