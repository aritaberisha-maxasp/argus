# How the ARGUS team works

## The weekly sprint

Sprints are one week, Monday to Sunday.

- **Sunday night, sprint review and planning proposal.** The PO posts one thread in the project with what got done, what carries over, the acceptance test results from QA, and a ranked list of proposed issues for the next sprint. Each proposed issue shows its requirement IDs, a size (S, M or L) and what it depends on.
- **Monday morning, planning.** Arita reorders, drops or adds issues by replying in that thread. Nothing starts until Arita approves the list. The PO then adds the approved issues to that sprint's milestone and labels them `status:ready`.
- **Every morning, daily digest.** The coordinator posts one message: what got done overnight, what is in progress, what is blocked, and what needs Arita today.
- **Sunday, retro.** The team holds its own retro at the end of each sprint, without Arita. Each role says what went well, what did not and what to change, and the team agrees on at most five action items, each with an owner role. The record goes in [docs/retros/](../retros/), lessons go into project memory, the PO puts the action items into the planning proposal, and any change to these docs comes to Arita as a PR.
- **When the sprint runs dry.** If no issue in the current sprint is ready, the night shift may refine the next top ranked backlog stories: QA writes test cases, the architect adds technical notes and the PO checks the Definition of Ready. No code and no branches for them until Arita approves the sprint. The night shift does this on its own, without waiting to be asked.

The team works 19:00 to 07:00 CET on weekdays and all weekend. To protect usage, at most six threads are open at the same time, and that count includes reviewer, compliance and QA threads. At most three of them are developer threads. When the cap is full, the next PR waits for a free slot before its checks start.

## How one issue flows

1. **PO** writes the issue with acceptance criteria and requirement IDs. When it meets the Definition of Ready it moves to **Ready**.
2. **Architect** adds technical notes if the issue touches the design. **QA** adds the test cases it will check.
3. A **developer** picks it up, moves it to **In progress**, opens a draft PR that says `Closes #<n>`, and keeps CI green.
4. When the code is complete the developer marks the PR ready for review and moves the issue to **In review**. The **reviewer** reviews. If the PR touches payments, bookings, the audit log, personal data or credentials, the **compliance reviewer** reviews as well. See [Reviews](#reviews) for how.
5. **QA** verifies the acceptance criteria, posts the evidence on the PR and moves the issue to **QA passed**.
6. The PR gets the label `ready-for-arita` and the issue moves to **Ready for Arita**. Arita reviews and merges, and the issue closes as **Done**.

If something blocks the issue, add the `blocked` label and say on the issue what it waits on.

### Working in parallel

- **Post the interface first.** When other stories will build on yours, post the interface (names, inputs, outputs, migration number) as a comment on the issue before writing the code.
- **Do not wait on one merge.** Once a base PR has its reviewer verdict, the next stories may start from its branch. Their PR targets that branch or main and says `Merge after #<n>` on the first line after the attribution block, so Arita knows the order.
- **One line of migrations.** The CI step "Migration chain" fails when two migrations point at the same earlier one or share a number. When it is red, point your new migration's `down_revision` at the highest migration in main or any open PR, give it the next free number and add `Merge after #<n>`. A green check is only as fresh as its run, so after a merge the author of every other open PR that adds a migration updates its branch from main, which runs the check again before Arita merges it.
- **Connect the pieces.** The PO plans the story that connects two pieces in the same sprint as the pieces. A PR that leaves something unconnected names the follow up issue in its description, and the PO decides that night whether it joins the sprint.
- **Closing keywords only when you mean it.** GitHub closes an issue when a merged PR or commit says `close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves` or `resolved` next to its number, even in the middle of a sentence. Use those words only for the issue the PR finishes. Anywhere else write "issue 2" or "#2" without them.

## Reviews

- **Never your own work.** The author thread never reviews or tests its own PR. The coordinator starts a separate thread for the reviewer, the compliance reviewer and QA, and the author fixes what they post.
- **The verdict is a PR comment.** GitHub does not let our sessions approve a PR, so Arita decided on 2026-10-16 that a comment counts as the approval. It starts with `Verdict: approved` or `Verdict: changes requested` and names the commit it covers. A push after the verdict needs a short re-check on the new commit.
- **Every PR gets a verdict before it is called ready**, including a quick backlog reorder.
- **Document only PRs** (spikes, ADRs, backlog, process docs) need a reviewer verdict but no QA evidence. The PR says "Docs only, no QA evidence needed" so the reason is visible. Small chores such as a rename get a short review too.
- **ADR numbers are claimed up front.** Before picking a number, check main and every open PR, and put the number in the PR title. When several threads start ADRs at the same time, the coordinator hands out the numbers.

## Board columns

Backlog, Ready, In progress, In review, QA passed, Ready for Arita, Done.

The board is driven by labels, because agents can update labels but cannot move cards on a GitHub Projects board. An open issue with no status label is in the Backlog. The labels `status:ready`, `status:in-progress`, `status:in-review`, `status:qa-passed` and `ready-for-arita` are the columns in between, and a closed issue is Done. An issue carries exactly one status label at a time: whoever moves it removes the old one, and when an issue closes its status label comes off.

Quick views for Arita:
- [Ready for Arita](https://github.com/aritaberisha-maxasp/argus/issues?q=is%3Aopen+label%3Aready-for-arita)
- [Questions for Arita](https://github.com/aritaberisha-maxasp/argus/issues?q=is%3Aopen+label%3Aneeds-arita)
- [Blocked](https://github.com/aritaberisha-maxasp/argus/issues?q=is%3Aopen+label%3Ablocked)
- [Current sprint](https://github.com/aritaberisha-maxasp/argus/milestones)

## Labels

- **Type:** `story`, `spike`, `bug`, `adr`, `chore`
- **Requirement area:** `req:IN`, `req:PR`, `req:AP`, `req:BK`, `req:PY`, `req:AR`, `req:BA`, `req:VM`, `req:EX`, `req:NF`
- **Role:** `role:po`, `role:architect`, `role:dev`, `role:qa`, `role:compliance`
- **Status:** `status:ready`, `status:in-progress`, `status:in-review`, `status:qa-passed`, `ready-for-arita`
- **Flow:** `needs-arita`, `blocked`
- **Size:** `size:S`, `size:M`, `size:L`

## How agents talk to each other

On GitHub, on the issue or PR the conversation is about. That keeps every discussion next to the work and visible to Arita. Anything urgent for Arita goes to the project thread with a clear question that can be answered in one word.

**Arita often answers on GitHub**, sometimes as a quote reply on the issue, and a thread is not woken by a GitHub comment. So:
- Before a thread replies to Arita, marks a question open or moves an issue, it re-reads every comment on its issue and its PR. A `needs-arita` label alone does not mean the question is still open: the digest reads the comments before it lists one.
- Each night shift, the coordinator looks for comments by Arita that are newer than the last agent comment on any open issue or PR, and sends each one to the thread that owns the work.

## What Arita looks at

1. The daily digest in the project chat.
2. The **Ready for Arita** column, the only one that needs action.
3. Questions labelled `needs-arita`.
4. The Sunday planning thread.
