# ARGUS team charter

Every Claude agent working on ARGUS reads this file first. It explains what we build, how the team works and the rules nobody breaks.

## What we build

ARGUS automates the finance back office of Max ASP GmbH: it classifies every email in the accounts inbox, books supplier invoices, outgoing invoices and Wise bank transactions in DATEV Rechnungswesen, bundles approved invoices into Wise payment runs twice a month, and keeps a full audit trail from receipt to payment.

The requirements live in [docs/requirements/requirements.md](docs/requirements/requirements.md). Every requirement has a stable ID (IN-04, AP-09, PY-04 and so on). Use those IDs everywhere.

## Who is who

- **Arita** is the product owner on the human side and the only person who merges. Arita relays business questions to Max ASP. Never contact Max ASP staff directly.
- **The coordinator** (the project's main Claude session) acts as Scrum Master: it routes work, starts threads and posts the daily digest.
- **Agents** work in their own project threads, one role per thread. Role cards are in [docs/team/roles/](docs/team/roles/).

## How work flows

See [docs/process/workflow.md](docs/process/workflow.md) for the full flow, board columns, labels and ceremonies. The short version:

1. Every piece of work starts as a GitHub issue that meets the [Definition of Ready](docs/process/definition-of-ready.md).
2. One issue, one branch, one PR. Open the PR as a draft early and link the issue with `Closes #<n>`.
3. Drive the PR until CI is green, review comments are handled and QA has posted test evidence.
4. Then add the label `ready-for-arita` and move the issue to that column. Arita reviews and merges.
5. A PR is done only when it meets the [Definition of Done](docs/process/definition-of-done.md).

## Rules nobody breaks

- **Never merge a PR, never enable auto merge, never push to `main`.** Arita merges everything.
- **Never skip, disable or weaken a test** to get CI green.
- **No real secrets anywhere** in the repo, issues, PRs, memory or logs. Use placeholders and environment variables. Real credentials for Microsoft 365, DATEV and Wise are handled by Arita.
- **No real customer, vendor or personal data in the repo.** Test fixtures are synthetic.
- **Segregation of duties is a product rule too:** ARGUS may prepare payments but must never be able to release them (NF-04). Code that could release a payment is a defect.
- **Write it where people can see it.** Decisions go in an ADR or on the issue, questions go on the issue or to Arita in the project, never only in a private message between agents.
- **When a requirement is unclear, ask, don't guess.** Post the question on the issue with the label `needs-arita` and carry on with something else.
- **Plain, natural language** in everything humans read: short sentences, no jargon where a normal word works, and never use a double hyphen or long dash as punctuation.

## Conventions

- Branch names: `<type>/<issue number>-<short-slug>`, for example `feat/42-email-classifier`. Types: `feat`, `fix`, `spike`, `docs`, `test`, `chore`.
- Commit messages: a short imperative subject, then a body that names the requirement IDs it covers.
- PR titles: plain language, what changes for the user of ARGUS, not the file names.
- Architecture decisions: one ADR per decision in [docs/adr/](docs/adr/), using the template there.

## Working hours

The team works after hours so Arita's day is free: 19:00 to 07:00 CET on weekdays and all weekend. Sprints run Monday to Sunday. Planning proposal goes out Sunday night, Arita approves on Monday morning, work starts Monday at 19:00.
