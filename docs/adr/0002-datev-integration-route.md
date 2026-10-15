# 0002: How ARGUS reaches DATEV Rechnungswesen

Status: proposed
Requirements: BK-01 to BK-07, AR-03 to AR-08, PR-02, PR-04, PR-05, VM-01 to VM-04, NF-01, NF-04, NF-06

## Context

ARGUS runs in Azure (ADR 0001). DATEV Rechnungswesen runs on a server inside Max ASP. ARGUS must read vendor and customer accounts with their IBANs, read booking history, create bookings with the invoice attached, and get a booking reference or a clear error back. It must never create or change vendor or customer accounts and never touch payments. Only interfaces DATEV supports are allowed (NF-06).

The full comparison is in the [DATEV spike report](../spikes/datev-access.md).

## Options

1. **DATEVconnect with a small bridge at Max ASP.** DATEVconnect is DATEV's free REST API on the DATEV server. A bridge inside Max ASP's network calls it and talks to ARGUS over outgoing HTTPS only. Pro: the only route that reads Kreditoren, Debitoren, IBANs and uncommitted postings, and returns a result per booking. Con: Max ASP's IT has to install and look after a small service.
2. **DATEV booking file (EXTF CSV) only.** ARGUS writes the file, Rechnungswesen imports it. Pro: simple, fully documented, testable without DATEV. Con: one way only, no reading of accounts, no result per booking, and someone has to start the import.
3. **DATEV cloud APIs** (Buchungsdatenservice, Belegbilderservice, Datenservice Export). Pro: nothing to run at Max ASP. Con: cannot read vendor or customer accounts, only committed history, needs DATEV Unternehmen online and DATEV partner approval.
4. **A third party DATEV gateway.** Pro: no bridge and no partner onboarding. Con: another company between Max ASP's books and ARGUS, with its own hosting and contract.

## Decision

We propose option 1: **DATEVconnect, reached through a small outgoing only bridge inside Max ASP's network.**

- ARGUS talks to DATEV only through one interface in code, the DATEV gateway. Every caller uses that, never DATEVconnect directly.
- Bookings are written as uncommitted batches or booking proposals, whichever DATEVconnect supports for Max ASP's version. Benjamin, or whoever books today, commits them in DATEV. This waits on Arita's answer to question 6 in the spike report.
- The invoice image and XML stay in ARGUS's locked document store. The DATEV booking carries a voucher link to it (DATEV Beleganzeige Drittanbieter), so there is one original and DATEV points to it.
- The bridge only allows a fixed list of read operations plus writing bookings. It refuses master data writes and anything in DATEV's payment modules.
- The DATEV booking file (option 2) is the fallback if DATEVconnect cannot write bookings for Max ASP's version. The gateway hides which route is used.
- Until we have access, development and CI use a fake DATEV that follows the DATEVconnect contract, and one contract test suite runs against the fake and, later, a DATEV test client at Max ASP.

Why: options 2 and 3 cannot read the vendor accounts and IBANs, so PR-02, PR-05, VM-03, VM-04 and AR-03 would need manual work. Option 4 adds a company we do not need if Max ASP can run a small service.

This decision depends on Arita's answers to the questions in the spike report. If Max ASP books through a tax advisor or runs DATEV with DATEVasp, we revisit it.

## Consequences

**Easier**
- Vendor matching, IBAN checks and the "resume when the account exists" rule all read live data from DATEV.
- Each booking gets a reference or a precise error, which feeds the exception queue (BK-06) and reconciliation (BK-07).
- Developers and CI never need DATEV access.

**Harder**
- Max ASP's IT installs DATEVconnect and the bridge, and gives the bridge a Windows user with limited DATEV rights.
- The bridge needs its own release, monitoring and an alert when DATEVconnect stops answering after DATEV updates.
- Voucher links need DATEV Beleganzeige Drittanbieter on each DATEV workstation that should show the image.

**Watch**
- Confirm with DATEV's own API specification which DATEVconnect accounting endpoints can write, and where the creditor IBAN lives.
- Check whether DATEV requires partner registration for a company's own DATEVconnect use.
- Keep the fake in step with the real DATEVconnect answers every time the contract tests run against the test client.
