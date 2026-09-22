# Judgment challenge: "insurance questions" in the assistant's scope

> **OWNER: WRITE THIS DOCUMENT YOURSELF — MAXIMUM 400 WORDS.** The framing below states the
> challenged instruction explicitly (the scenario contains no quoted senior-leader line, so the
> instruction is the capability mandate itself); keep that framing, then argue it in your own
> words. Delete this notice when done.

**The instruction I would challenge.** The senior leader's requirement, as stated in the scenario's
capability list: *"the assistant must handle … insurance/general questions."* Read plainly, that
puts insurance answers — including, in most patients' minds, "does my policy cover this?" — inside
an autonomous assistant from day one.

**What I accept:** <general insurance information from approved content: accepted insurers, what to
bring, how pre-authorization works — useful, safe, and fully grounded in the KB; implemented today
(`kb/insurance-general.md`, eval `hp_insurance_general`)>.

**What I would change:** <individual coverage, cost, and eligibility determinations are excluded;
the assistant routes them to the Insurance Desk; implemented today (prompt rule 3, escalation
category `insurance_determination`, eval `unsafe_insurance_coverage_determination`)>.

**Why:** <liability and trust: a wrong coverage answer costs the patient money or a missed
procedure; the KB cannot hold individual policy facts; regulatory exposure; one confident wrong
answer damages the whole assistant's credibility>.

**How I preserve speed while protecting Apex:** <ship general insurance Q&A now; instrument what
share of insurance questions are coverage determinations; design the Insurance Desk integration as
phase two, behind the same confirmation-and-verification gate, so the roadmap is explicit rather
than silently over-promised>.
