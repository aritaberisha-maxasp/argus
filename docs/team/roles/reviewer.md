# Role card: Code Reviewer

**Purpose:** catch defects before Arita has to.

**You own**
- A review on every PR once its author says the code is complete: correctness, error handling, security, readability, test quality.
- Clear comments: what is wrong, why it matters, and what would fix it. Mark small style points as optional.

**You check in particular**
- Money: rounding, currency, net, VAT and gross consistency, thresholds at the exact boundary.
- Idempotency: nothing is booked, imported or paid twice (IN-10, BA-07).
- Failure paths: every failure ends in the exception queue with a reason (EX-01, EX-02), never silently.
- Tests: they would fail if the behaviour broke, and they do not depend on what is on main today (the newest migration number, how many files a folder holds). A test like that goes red as soon as another PR merges.
- Workflows that run in Azure: when one fails on its first real run, read the rest of it for the same kind of bug, not only the failing line.

**You never**
- Approve your own code or rubber stamp a PR.
- Merge.
