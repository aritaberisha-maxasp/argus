# Definition of Done

A PR is ready for Arita only when all of this is true:

- **It does what the issue asks.** Every acceptance criterion is met, and QA has posted evidence on the PR (test output, sample input and result). A document only PR says "Docs only, no QA evidence needed" instead.
- **It is tested.** New behaviour has automated tests. Business rules (amounts, dates, thresholds, matching) have tests for the edge cases, not just the happy path.
- **CI is green.** No skipped or weakened tests.
- **It is reviewed.** The reviewer approved it, from a thread other than the author's, with a verdict comment on the PR that names the commit it covers. If it touches payments, bookings, the audit log, personal data or credentials, the compliance reviewer approved it too.
- **It is traceable.** The PR names the requirement IDs it covers, and the tests reference them.
- **It leaves an audit trail.** Any step that changes the state of an invoice, booking or payment writes to the audit log (NF-01, NF-05).
- **It is safe.** No secrets, no real personal or company data, nothing that could release a payment (NF-04).
- **It is connected, or says what is not.** If the feature only works once another story connects it, the PR names that follow up issue.
- **It is documented.** Behaviour a user or operator would notice is described in the docs, and any design decision has an ADR.
- **The PR description is easy to review on a phone:** what changed in plain language, why, how it was tested and what the risks are.
