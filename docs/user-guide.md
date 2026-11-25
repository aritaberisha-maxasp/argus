# ARGUS user guide

Short answers for the people who work with ARGUS every day at Max ASP: the finance team, the approvers and the admins. Arita passes new sections on to Benjamin. Each section names the requirement it is about, so the team can find the rule behind it.

## IBAN holds

Covers PR-05 and VM-04.

### Why ARGUS holds an invoice

When ARGUS has found the vendor of an incoming invoice, it compares every IBAN printed on the invoice with the bank accounts DATEV holds for that vendor. If even one of them is not in DATEV, ARGUS holds the invoice and puts it in the exception queue as "Possible fraud: the bank account differs from DATEV". A held invoice is not sent for approval and is never paid.

A changed bank account is one of the most common fraud tricks: someone sends an invoice, or an email that looks like it comes from the vendor, with a new IBAN. So ARGUS never takes bank details from an invoice or an email. The bank details in DATEV are the only ones that count.

### What to do

1. **Confirm the change with the vendor first.** Call the vendor on a phone number Max ASP already knows, for example from an older invoice or from DATEV. Never use a phone number or email address from the invoice or the email that announces the change.
2. **If the vendor confirms the new IBAN,** add it to the vendor's account in DATEV. You do nothing in ARGUS. On its next run ARGUS reads DATEV again, and once DATEV holds every IBAN the invoice states, the hold ends by itself and the invoice goes on to approval. The case trail on the invoices page then shows that ARGUS ended it as "fixed in DATEV".
3. **If the vendor does not confirm it, or you are not sure,** choose "Reject the invoice" on the invoices page, with a short reason. The invoice is then never approved, booked or paid.

Only ARGUS's own check of DATEV can end a hold as "fixed in DATEV". There is no button for it, for nobody, admins included. This way a hold never ends without DATEV really holding the bank account. The one thing a person can do in ARGUS is reject the invoice.

### A bank change letter with the old and the new IBAN

Many "we changed banks" letters print both the old and the new IBAN. ARGUS checks every IBAN on the invoice, so the hold ends only when DATEV holds both of them.

If you add the new IBAN to DATEV and delete the old one at the same time, the invoice stays held, because the old IBAN it states is no longer in DATEV. Once you have confirmed the change with the vendor, you have two ways out:

1. **Reject the invoice in ARGUS** and ask the vendor to send it again showing only the new IBAN. The new copy comes in as a new invoice, and ARGUS lets it through once the new IBAN is in DATEV. If it has the same invoice number and amount, ARGUS first shows it in the exception queue as a possible duplicate of the rejected one. Check that it is the corrected copy and choose "Not a duplicate, carry on", with that as the reason.
2. **Keep the old IBAN in DATEV for a while,** next to the new one. The hold ends on ARGUS's next run. Then remove the old IBAN from DATEV, before the next payment run, so the money goes only to the new account.
