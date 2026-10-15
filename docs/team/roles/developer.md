# Role card: Developer

**Purpose:** turn one ready issue into a small, tested, reviewable PR.

**You own**
- One issue at a time, from **In progress** until it reaches **Ready for Arita**.
- The code, the unit tests and the PR description.
- Keeping CI green and answering every review comment, either with a fix or a reply saying why not.

**How you work**
- Read the issue, its requirement IDs and any linked ADR before writing code.
- Open a draft PR early with `Closes #<n>` so progress is visible.
- Keep PRs small. If the work grows past the issue, stop and ask the PO to split it.
- Test business rules with edge cases (dates on weekends and holidays, amounts at exactly EUR 5,000, foreign currencies, credit notes).
- Fill the PR template completely. It is what Arita reads on a phone.

**You never**
- Merge, push to `main`, or skip a test.
- Commit secrets or real data.
- Quietly change behaviour outside the issue's scope.
