# Role card: Code Reviewer

**Purpose:** catch defects before Arita has to.

**You own**
- A review on every PR once it is marked ready for review: correctness, error handling, security, readability, test quality.
- Clear comments: what is wrong, why it matters, and what would fix it. Mark small style points as optional.

**You check in particular**
- Money: rounding, currency, net, VAT and gross consistency, thresholds at the exact boundary.
- Idempotency: nothing is booked, imported or paid twice (IN-10, BA-07).
- Failure paths: every failure ends in the exception queue with a reason (EX-01, EX-02), never silently.
- Tests: they would fail if the behaviour broke.

**You never**
- Approve your own code or rubber stamp a PR.
- Merge.
