# How the ARGUS team works

## The weekly sprint

Sprints are one week, Monday to Sunday.

- **Sunday night, sprint review and planning proposal.** The PO posts one thread in the project with what got done, what carries over, the acceptance test results from QA, and a ranked list of proposed issues for the next sprint. Each proposed issue shows its requirement IDs, a size (S, M or L) and what it depends on.
- **Monday morning, planning.** Arita reorders, drops or adds issues by replying in that thread. Nothing starts until Arita approves the list. The PO then adds the approved issues to the sprint on the board.
- **Every morning, daily digest.** The coordinator posts one message: what got done overnight, what is in progress, what is blocked, and what needs Arita today.
- **Sunday, retro.** A few lines on what to keep, what to change. Lessons go into project memory so the next sprint starts smarter.

The team works 19:00 to 07:00 CET on weekdays and all weekend. To protect usage, at most three developer threads run at the same time.

## How one issue flows

1. **PO** writes the issue with acceptance criteria and requirement IDs. When it meets the Definition of Ready it moves to **Ready**.
2. **Architect** adds technical notes if the issue touches the design. **QA** adds the test cases it will check.
3. A **developer** picks it up, moves it to **In progress**, opens a draft PR that says `Closes #<n>`, and keeps CI green.
4. When the code is complete the developer marks the PR ready for review and moves the issue to **In review**. The **reviewer** reviews. If the PR touches payments, bookings, the audit log, personal data or credentials, the **compliance reviewer** reviews as well.
5. **QA** verifies the acceptance criteria, posts the evidence on the PR and moves the issue to **QA passed**.
6. The PR gets the label `ready-for-arita` and the issue moves to **Ready for Arita**. Arita reviews and merges, and the issue closes as **Done**.

If something blocks the issue, add the `blocked` label and say on the issue what it waits on.

## Board columns

Backlog, Ready, In progress, In review, QA passed, Ready for Arita, Done.

## Labels

- **Type:** `story`, `spike`, `bug`, `adr`, `chore`
- **Requirement area:** `req:IN`, `req:PR`, `req:AP`, `req:BK`, `req:PY`, `req:AR`, `req:BA`, `req:VM`, `req:EX`, `req:NF`
- **Role:** `role:po`, `role:architect`, `role:dev`, `role:qa`, `role:compliance`
- **Flow:** `ready-for-arita`, `needs-arita`, `blocked`
- **Size:** `size:S`, `size:M`, `size:L`

## How agents talk to each other

On GitHub, on the issue or PR the conversation is about. That keeps every discussion next to the work and visible to Arita. Anything urgent for Arita goes to the project thread with a clear question that can be answered in one word.

## What Arita looks at

1. The daily digest in the project chat.
2. The **Ready for Arita** column, the only one that needs action.
3. Questions labelled `needs-arita`.
4. The Sunday planning thread.
