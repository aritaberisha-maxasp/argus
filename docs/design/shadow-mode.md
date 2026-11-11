# Shadow mode

Covers: IN-04, IN-06, IN-09, NF-01. Issue: #72. Code: `src/argus/intake/mode.py`, `src/argus/intake/shadow.py`. Migration: 0012.

Arita agreed on #5 that ARGUS first runs two weeks on real mail without acting. A person keeps sorting the accounts Inbox. ARGUS only says what it would have done. The thresholds are then set from that data, so that no invoice is ever sent to Other.

## The two modes

| | Shadow (the default) | Live |
|---|---|---|
| Stores every email and its attachments | yes | yes |
| Sorts every email with the model and our checks | yes, recorded as a proposal | yes, recorded as the decision |
| Opens invoice cases and exceptions | no | yes |
| Forwards payment reminders | no | yes |
| Moves emails into the ARGUS folders | no | yes |
| Deletes emails from ARGUS/Removed after 30 days | no | yes |
| Records where the person put each email | yes | no |

The mode lives in the database (table `intake_mode`), not in an environment variable, so a deploy can never switch it by accident. ARGUS reads it before each email and right before every move, forward and delete. Switching back to shadow mode therefore stops ARGUS within one email.

## Who can switch

Only the admin role (Arita, ADR 0008) can switch, through `PUT /intake/mode`. Everyone with an ARGUS role can read the mode at `GET /intake/mode`.

The switch needs a short reason, for example "shadow report of 21 Nov, no invoice to Other". The database adds rules of its own: only a person can change the mode, never a job, and every change is written into the audit log as `intake_mode_changed` with the mode before and after, the person's Entra object ID, their name and sign in address as they were at that moment, and the reason (ADR 0008: the object ID alone names nobody once the account is gone). The name and reason must come with the switch itself, so a switch never reuses an earlier one's. The app cannot write that event itself.

In shadow mode the database also refuses to classify an email. That step is where cases and exceptions are opened, so nothing downstream starts while ARGUS only proposes.

## What the person did

Once an email with a proposal has left the Inbox, the sweep asks Graph which folder it is in now. It only reads: it creates no folder and moves nothing. The folder list `ARGUS_SHADOW_FOLDERS` says what each folder means, for example:

```json
{"Rechnungen/Eingang": "incoming_invoice", "Rechnungen/Ausgang": "outgoing_invoice", "Mahnungen": "payment_reminder", "Newsletter": "other"}
```

- An email in Deleted Items, or gone from the mailbox, counts as Other.
- An email in a folder that is not on the list is recorded as "A folder not on the list", without a category. The report shows it in its own row instead of guessing. ARGUS keeps the folder's Graph ID, never its name, so when the list is set late or fixed, the next sweep gives those emails their category from the same folder. That is the only later change the database allows, and the audit log shows it with `mapped_later`.
- Paths start at the top of the mailbox. A subfolder of the Inbox is written with the Inbox's own name, for example `Posteingang/Rechnungen` in a German mailbox.
- A name with anything but letters and spaces is refused when ARGUS starts, so a bad list never makes each sweep fail. A Graph error while looking up the folders counts as one failure, and the next sweep tries again.
- Both the proposal (`email_proposed`) and the person's folder (`email_sorted_by_person`) are written once and go into the audit log.

Max ASP has to send the folder list. It is an item in the [go live runbook](../runbooks/go-live.md). Folder names can only use letters and spaces for now, the same rule as for the ARGUS folders.

## The report

`GET /intake/shadow-report`, for finance and admin. It shows:

- how many emails got a proposal, how many the person has sorted and how many are still waiting;
- per category the person chose: how many emails, how often ARGUS proposed the same route, how often it would have asked a person (manual review) and how often it proposed something else, with the count per route;
- every email the person treated as an invoice that ARGUS would have sent to Other, with the model's category and confidence and our checks' category;
- every email in a folder not on the list that ARGUS would have sent to Other, because it may be an invoice too;
- the folder list it used, and warnings when the folder list is missing or emails sit in folders not on it.

Both lists and the warnings must be empty before Arita switches to live mode. A report without a folder list never looks clean: it says so.

## After the switch

An email that got a proposal and is still in the Inbox when Arita switches is sorted again in live mode, from scratch, and handled like any new email. The person's folder is only recorded for emails ARGUS never took on itself, so the report never counts ARGUS's own filing as the person's work.
