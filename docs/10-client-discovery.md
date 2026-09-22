# Client discovery questions (Part 10)

Ordered within each group by how much the answer changes scope.

## Systems and integration
1. Which scheduling system holds appointments, what API does it expose for read, book, reschedule, cancel — and does a write return a record version or confirmation id?
2. Is there an independent read path (reporting replica, HL7/FHIR feed) we can use to verify writes, and what is its lag?
3. Which identity provider will patients authenticate with, and can the assistant receive a verified patient id from it?
4. Are writes to the scheduling system idempotent, and how are duplicate requests handled today?
5. What are the rate limits and maintenance windows of the scheduling system?

## Data, privacy, regulatory
6. Which data is the assistant allowed to see (appointments only, or clinical data), and who signs off on that scope?
7. Where must patient data be processed and stored (in-Kingdom only), and which external processors are already approved?
8. Has the privacy office assessed sending conversation text to an external model provider, and under which agreement?
9. How long must conversation and audit records be retained, and who may read message text?

## Operations
10. Who owns the knowledge base content, how is approval recorded today, and how often does it change?
11. Who staffs escalations, during which hours, and what is the target response time?
12. What are today's volumes by channel and by intent (booking, rescheduling, cancellation, questions), and the peak hour?
13. Which languages and dialects do patients use, and what share of conversations are Arabic?
14. What is the process for a patient complaint about a wrong booking, and who communicates with the patient?

## Success and commercial
15. What does success look like at 90 days: containment rate, booking completion rate, patient satisfaction, cost per interaction, or reduced call-center load?
16. What is the pilot population and the criteria to expand beyond it?
17. What budget envelope exists for model usage and for human review?
18. Which existing vendor or platform constraints (cloud provider, contact center tooling) must the solution fit into?
