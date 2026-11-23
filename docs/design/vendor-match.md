# Matching invoices to vendor accounts (#35)

Covers PR-02, VM-02 and VM-03. Code: `src/argus/intake/vendors.py`.

## What it does

After intake opens a case for an incoming invoice or credit note (#30), ARGUS looks up the vendor account (Kreditor) in DATEV and writes its number on the case (`invoice_case.partner_account`). It reads DATEV through the gateway only. The gateway has no way to create or change a vendor account, so ARGUS never does (VM-01).

## How a vendor is found

ARGUS reads all vendor accounts and all customers' VAT IDs once per pass and compares them with what it read on the invoice. Only values read without a flag count: a VAT ID or IBAN a person still has to check is never used, because a wrong match sends the invoice to the wrong approver.

| Order | Rule | Recorded as |
|---|---|---|
| 1 | The VAT ID on the invoice belongs to one vendor account, and that account also has the IBAN on the invoice | `vat_id_and_iban` |
| 2 | The VAT ID belongs to one vendor account, the IBAN differs or is missing | `vat_id` |
| 3 | No vendor account has the VAT ID, but one has the IBAN | `iban` |
| 4 | Neither, but one vendor account has the same name (legal form, case and umlauts ignored) | `name`, marked weaker |

A match fails, and nothing weaker is tried, when:

- the VAT ID is Max ASP's own (PO note on #35, #106);
- the VAT ID belongs to a customer account. It still counts when a vendor account has it too and the IBAN confirms that account, because a company can be both;
- two vendor accounts fit equally well;
- the IBAN or name points to a vendor account that holds a different VAT ID than the invoice.

When the VAT ID matches but the IBAN differs, the match stands, and the IBAN check below blocks the invoice.

Every match writes `vendor_matched` into the audit log with the account number, the rule (`by`) and `weaker: true` for a name only match. No name, VAT ID or IBAN goes into the log.

## When there is no match, or no approver

- **No vendor account fits:** the case goes to the exception queue as `unknown_vendor`. The detail tells Benjamin what to create in DATEV and shows the IBAN masked (`DE ... 0001`). Benjamin hears about it through the daily digest of open exceptions (EX-03).
- **The vendor account has no row in the approver table (#34):** `vendor_without_approver` (VM-02).

Every pass looks at these two exceptions again:

- Once the vendor exists in DATEV, ARGUS writes the account on the case and resolves `unknown_vendor` as `fixed_in_datev`.
- If that vendor has no approver yet, it opens `vendor_without_approver` first, in the same transaction, so the case never leaves the queue in between.
- Once the vendor has an approver, ARGUS resolves `vendor_without_approver` as `retried`.

The case then goes back to where it was and carries on. Nobody has to touch it (VM-03). If a person resolved the exception first, for example by rejecting the invoice, ARGUS leaves it alone.

Every pass also looks at matched cases still in Received whose vendor has no approver row. That happens when a person presses "Checked, carry on" on `vendor_without_approver` on the invoices page (#62), or when an approver row is removed before approval. ARGUS opens `vendor_without_approver` again, so no case reaches approval without an approver (VM-02, code review of PR 182). This check needs no DATEV, so it also runs while DATEV does not answer. For `unknown_vendor`, carry on brings the case back without a vendor account, and the next pass opens `unknown_vendor` again.

## When the IBAN differs (#36)

Covers PR-05 and VM-04. A changed bank account is a classic fraud pattern, so once the vendor account is found ARGUS compares every IBAN the invoice states with the IBANs DATEV holds for that account. When any of them is not among them, the case goes to the exception queue as `iban_mismatch`, marked as possible fraud. This also covers an IBAN that belongs to another vendor account; the detail names that account. Only an invoice that states no IBAN at all is not compared.

**Flags stop finding, never blocking** (compliance and code review of PR 185). A flagged IBAN is not used to find the vendor, but it is always checked. That covers every flag: several values (the usual "we changed banks" letter that prints the old and the new IBAN), low confidence, found only by the language model, readers disagree and wrong check digits. The readers keep every IBAN they saw in the field's `values` (several values, readers disagree), so the check does not depend on the note text; for readings stored before that, the IBANs the note names count too. The audit facts say `read_with_flag` and how many IBANs were stated and differ. A flagged IBAN that is on file carries on as before.

- **The invoice is blocked.** An open exception stops every status change of the case in the database (migration 0002), so it is never sent for approval and never goes into a payment run.
- **ARGUS never takes bank details from an invoice (VM-04).** The gateway has no way to change a vendor account, and nothing in ARGUS reads the invoice's IBAN as new master data.
- **How it ends.** Benjamin checks with the vendor on a phone number Max ASP already knows. If the new IBAN is right, Benjamin changes it in DATEV; every pass looks at open IBAN mismatches again and resolves the exception as `fixed_in_datev` once DATEV holds every IBAN the invoice states on that vendor account. If it is not right, a person rejects the invoice on the invoices page. The page offers only reject: "fixed in DATEV" is ARGUS's own check, never a click. The database already allows only these two endings (migration 0002).
- **Masked everywhere.** The detail shows IBANs as country and last four characters (`DE ... 0006`). The audit log gets the account number, `possible_fraud`, how many IBANs are on file and any other account that holds the invoice's IBAN, never an IBAN. The pass logs only case IDs.

**Cases matched before this check.** The check runs when a case is matched. A case that vendor matching (#35) matched before this change deployed, and that still waits in Received, was never compared. Nothing is live yet and every demo reset empties the cases, so there are none to worry about; this only matters if real cases were matched before this deploys.

The payment run (from #40) must still pay only to the IBAN DATEV holds when the run is prepared, not the one on the invoice. This check stops a changed IBAN at the door; that rule stops a change made in DATEV after approval from reaching Wise unseen.

## When it runs

At the end of every live sweep, after filing, so it needs no job of its own. Right after it, the sweep proposes the G/L account and tax key of each matched case ([account-proposal.md](account-proposal.md), #37). It asks DATEV only when there is a vendor to find or an IBAN to check again: a new case, an open `unknown_vendor` or an open `iban_mismatch`. When DATEV does not answer, those wait for the next sweep; the approver checks still run. In shadow mode no cases are opened, so there is nothing to match.

The job builds the gateway from `ARGUS_DATEV_URL` and `ARGUS_DATEV_CLIENT` (`argus.connections`). The demo points them at the fake DATEV. Live they are set at go live ([go live runbook](../runbooks/go-live.md)); until then cases wait without a vendor account.

The demo reset (`python -m argus.demo reset`) fills the approver table from the synthetic `tests/fixtures/master-data/approvers.csv`, so demo invoices carry on to approval. Vendor 70008 has no approver there on purpose, so E13 shows "The vendor has no approver".

## Testing against the fake DATEV

The fake DATEV has a test control `POST /_fake/creditors` that stands in for Benjamin creating a vendor by hand. `PUT /_fake/creditors/{number}/bank-accounts` stands in for Benjamin changing a vendor's bank details. ARGUS never calls it: the gateway's allow list only knows DATEVconnect's paths. `/_fake/reset` removes such vendors again.
