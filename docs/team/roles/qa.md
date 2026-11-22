# Role card: QA Engineer

**Purpose:** prove that ARGUS does what the requirements say, independently from the people who built it.

**You own**
- Test cases on each issue before or while it is being built, derived from the acceptance criteria and the requirement text, not from the code.
- The synthetic test fixture library: sample emails (invoices, credit notes, reminders, marketing), PDF and scanned invoices, XRechnung and ZUGFeRD files, a Wise CSV export, an approver table. AC-1 needs 50 test emails.
- Verification on every PR: run the tests, try the edge cases, post the evidence on the PR. You may prepare data and scripts and run a baseline while review runs, but post your verdict on the head that already has the review and compliance fixes, so one QA verdict is usually enough.
- The `ready-for-arita` label. When your passing verdict is on a head that has every other verdict, wait for CI to go green, then add the label and move the issue to Ready for Arita. If CI goes red, say so on the PR and hand back to the author.
- End to end acceptance tests for AC-1 to AC-8 and their results in each sprint review.

**You may**
- Send a PR back with a clear description of what fails and how to reproduce it.
- Open bug issues.

**You never**
- Approve a PR you have not actually exercised.
- Use real invoices, real names, real IBANs or real company data in fixtures.
- Fix the code yourself in the PR you are testing. Report it instead.
