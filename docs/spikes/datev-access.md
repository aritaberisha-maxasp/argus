# Spike: how ARGUS can book into DATEV Rechnungswesen

Issue: #2
Requirements: BK-01 to BK-07, AR-03 to AR-08, PR-02, PR-04, PR-05, VM-01 to VM-04, NF-04, NF-06 (BA-02 benefits from the same route)
Status: findings from public DATEV material and integrator documentation. Nothing here was tried against a real DATEV installation. Points marked **to confirm** need DATEV's own API specification or a look at Max ASP's installation.

## The short answer

DATEV Rechnungswesen runs on a server inside Max ASP. The only vendor supported interface that can both **read** vendor and customer accounts (with bank details) and **write** bookings into that in house installation is **DATEVconnect**, a free DATEV add on that offers a REST API on the DATEV server itself. A cloud service cannot call it directly, so ARGUS needs a small **bridge** inside Max ASP's network that talks to DATEVconnect locally and to ARGUS over an outgoing HTTPS connection.

DATEV's cloud APIs (Buchungsdatenservice, Belegbilderservice, Datenservice Export) are the alternative. They need no machine at Max ASP, but they cannot read vendor and customer accounts back, they need DATEV Unternehmen online and a DATEV partner approval, and bookings only reach Rechnungswesen when someone picks them up there.

The ADR [0002](../adr/0002-datev-integration-route.md) proposes DATEVconnect plus the bridge, with the DATEV CSV booking file as the fallback.

## What ARGUS needs from DATEV

| Need | Requirements | Direction |
| --- | --- | --- |
| Find the vendor account (Kreditor) by VAT ID, IBAN or name | PR-02, VM-02, VM-03 | read |
| Know the vendor's stored IBAN, to catch a changed IBAN | PR-05, VM-04 | read |
| Find the customer account (Debitor) | AR-03 | read |
| Notice when Benjamin creates a missing account, so the case resumes | VM-03, AR-03 | read, repeatedly |
| Read past bookings of a vendor to propose G/L account and tax key | PR-04 | read |
| Create a booking with all fields from BK-02 and AR-05 | BK-01, BK-02, AR-04, AR-05 | write |
| Attach the invoice image and XML to the booking | BK-03, AR-06 | write |
| Book credit notes and link them | BK-05, AR-08 | write |
| Get a clear error for closed periods or unknown accounts | BK-06 | read the result |
| Get a booking reference back | BK-07 | read the result |
| Never create or change vendor or customer accounts | VM-01, VM-04 | must not write |

## The options

### 1. DATEVconnect (on premises REST API)

What it is: a DATEV product (article 95280, listed at EUR 0 a month) that is installed on exactly one server where DATEV Rechnungswesen runs. It serves a REST API on port 58452 of that server, for example `https://<server>:58452/datev/api/accounting/v1/clients/`. It covers DATEV Kanzlei Rechnungswesen and DATEV Mittelstand Faktura mit Rechnungswesen.

What it can do, as far as public sources show:
- **Read** clients, fiscal years, G/L accounts (Sachkonten), creditors and debitors, terms of payment, cost centers and account postings (Buchungssätze). An open source SDK lists 34 accounting endpoints, including creditors, debitors, general ledger accounts, accounting records and accounting sequences.
- **Read bank details.** The SDK places bank details (Bankverbindungen) in the master data API, not the accounting API. Whether a creditor's IBAN is reachable from the creditor record is **to confirm**.
- **Write booking proposals** (Buchungsvorschläge). ELO documents this as possible since DATEV version 13.1. A person then accepts the proposals in Rechnungswesen.
- **Write booking batches** (Buchungsstapel). Integrators such as klardaten list "creating accounting sequences" as a write action. Exact behaviour **to confirm**.

What it cannot do or what is awkward:
- It only listens inside Max ASP's network. ARGUS in Azure needs a bridge or a VPN.
- It authenticates with a Windows user on the DATEV server (Basic authentication is gone since DATEV version 15), so the bridge runs under a dedicated Windows account with only the DATEV rights it needs.
- The voucher image is not part of the accounting API. See "Voucher images" below.
- After DATEV updates the DATEVconnect service sometimes has to be restarted. The bridge should report when DATEVconnect stops answering.

### 2. DATEV booking file (DATEV format, EXTF CSV)

What it is: DATEV's published import format. ARGUS writes a CSV file per batch (format category 21, Buchungsstapel) and Rechnungswesen imports it.

What it can do:
- Carry every booking field ARGUS needs: account, contra account, tax key, document date, document number (Belegfeld 1), amounts, currency, due date and a voucher link field (Beleglink).
- Bookings land as an uncommitted batch, which a person can check and then commit.
- Fully documented by DATEV, stable for years, easy to generate and to test without any DATEV access.

What it cannot do:
- It is one way. ARGUS gets no booking reference back and no error per booking. BK-06 and BK-07 would need a second step that reads postings back.
- Somebody, or a scheduled DATEV job, has to trigger the import inside Rechnungswesen. Whether that can run without a person is **to confirm** with Max ASP.

This option works well as a fallback and as the format the cloud Buchungsdatenservice also accepts.

### 3. DATEV cloud APIs (developer.datev.de)

What it is: REST APIs in DATEV's data centre, called with OAuth. The relevant ones:
- **Buchungsdatenservice** (`accounting:extf-files`): upload the same EXTF booking file. DATEV places it in the data centre and it is picked up in Rechnungswesen as a batch.
- **Belegbilderservice** (`accounting:documents`): upload voucher images into DATEV Unternehmen online, where they are linked to the bookings.
- **Datenservice Export Rechnungswesen** (`accounting:dataexchange`): read journal entries, chart of accounts, fiscal years, terms of payment. Only committed (festgeschriebene) data is available. Open items are in beta.
- **DXSO jobs** (`accounting:dxso-jobs`): booking proposals with voucher images into DATEV Unternehmen online.

What it can do:
- No machine at Max ASP, everything is HTTPS from Azure.
- Voucher images are handled by DATEV and shown with the booking.

What it cannot do:
- **Read vendor and customer accounts.** Integrators report that contacts can be created or updated through the booking file but never read back. That breaks PR-02, PR-05, VM-03, VM-04 and AR-03, which all depend on reading the accounts and their IBANs.
- Booking history is only readable after it is committed, which may lag weeks behind, so PR-04 would work on old data.
- It needs DATEV Unternehmen online with a Bestand for Max ASP, a MyDATEV client registration, the Buchungsdatenservice right for the people involved, and an app that DATEV has approved. Building our own app means going through DATEV's partner onboarding, which takes time and costs fees.
- Writing contacts through the booking file would let ARGUS change master data, which VM-01 and VM-04 forbid. We would have to make sure we never send that format.

### 4. Third party gateways (klardaten, maesn and similar)

Commercial services that expose DATEVconnect or the cloud APIs through their own hosted API. They save us the bridge or the partner onboarding, but they put a further company between Max ASP's books and ARGUS, with their own hosting (NF-03 must be checked), contracts and costs. We keep them as a plan B if DATEV partner rules block options 1 or 3.

## Comparison

| | DATEVconnect + bridge | EXTF file only | DATEV cloud APIs |
| --- | --- | --- | --- |
| Read Kreditoren and Debitoren | yes | no | no |
| Read the stored IBAN | yes, **to confirm** where | no | no |
| Read booking history (PR-04) | yes, including uncommitted | no | committed only |
| Write bookings | yes, proposals or batches | yes, as a batch | yes, as a batch |
| Booking reference and errors back (BK-06, BK-07) | yes | no | job status only |
| Voucher image (BK-03, AR-06) | via voucher link or DATEV DMS | via voucher link | yes, via Unternehmen online |
| Needs a machine at Max ASP | yes, small | no, but a file has to get there | no |
| Needs DATEV Unternehmen online | no | no | yes |
| Needs DATEV partner approval | **to confirm** | no | yes |
| Vendor supported (NF-06) | yes | yes | yes |

## Voucher images (BK-03, AR-06)

ARGUS keeps every original document unaltered in locked storage (ADR 0001). Three supported ways to show it on the DATEV booking:

1. **Voucher link (DATEV Beleganzeige Drittanbieter, article 63183).** The booking carries a link (Beleglink) to the document in a third party archive, and DATEV opens it from the booking. ARGUS would be that archive. ELO is supported out of the box. Other systems are set up with a command entry on each DATEV workstation. One licence per Rechnungswesen licence. Not usable with DATEVasp. **To confirm** that a link to a web address is allowed.
2. **DATEV DMS through DATEVconnect Dokumentenmanagement (article 63218).** The bridge stores the PDF and XML in DATEV DMS and links it to the booking. Only works if Max ASP uses DATEV DMS.
3. **Belegbilderservice (cloud).** Images go to DATEV Unternehmen online. Only makes sense together with option 3.

Our proposal is option 1, because the original stays in one place (ARGUS's locked store) and DATEV just points to it. Whether Max ASP already uses DATEV DMS or Unternehmen online changes this, so it is a question below.

## What the bridge looks like

- A small Windows service, installed by Max ASP's IT on the DATEV server or on another server that can reach port 58452.
- It only opens **outgoing** HTTPS connections to ARGUS in Azure. No inbound firewall rule, no VPN.
- It asks ARGUS for work, calls DATEVconnect locally and sends the result back. All business logic stays in ARGUS. The bridge has no rules of its own, so it rarely changes.
- It may only call a fixed list of DATEVconnect operations: read clients, fiscal years, accounts, creditors, debitors, bank details and postings, and write booking proposals or batches. Everything else is refused in the bridge itself, including master data writes (VM-01, VM-04) and anything in DATEV's payment modules (NF-04).
- It logs every call with the case ID, so the audit trail (NF-01) covers the DATEV step too.
- It signs in to ARGUS with its own certificate or Entra identity and to DATEVconnect with a dedicated Windows user that has only the DATEV rights it needs.

## Building and testing without the real DATEV

ARGUS talks to DATEV only through one interface in our code, the **DATEV gateway**. It has these operations:

| Operation | Used for |
| --- | --- |
| `list_vendors()`, `get_vendor(account)` with bank details | PR-02, PR-05, VM-02 to VM-04 |
| `list_customers()`, `get_customer(account)` | AR-03 |
| `list_gl_accounts()`, `list_tax_keys()` | BK-02, AR-04 |
| `list_postings(account, from, to)` | PR-04 |
| `submit_booking(booking, voucher_link)` returning a reference or a typed error | BK-01 to BK-07, AR-04 to AR-08 |
| `get_booking(reference)` | BK-07, reconciliation |

Three implementations:

1. **Fake DATEV**, for development and CI. Runs in Docker, serves the same JSON shapes as DATEVconnect's accounting API, and is filled from synthetic fixtures (an invented client "Muster GmbH" with invented Kreditoren, Debitoren, IBANs and postings). It can be told to fail the way DATEV fails: closed period, unknown account, duplicate document number, DATEVconnect not answering.
2. **DATEVconnect through the bridge**, for production.
3. **EXTF file writer**, the fallback. It is tested against DATEV's published format rules.

One **contract test suite** runs against every implementation. In CI it runs against the fake. Once Max ASP gives us a DATEV test client, Arita (or Max ASP's IT) runs the same suite against the bridge there. If the real DATEV answers differently, we fix the fake first, so the fake never drifts from the real thing.

Until we have DATEV's OpenAPI files for DATEVconnect, the fake follows the field names we can confirm from public material and is marked as provisional.

## Questions for Arita

These go on issue #2 with the label `needs-arita`. They decide between the options.

1. **Product and version.** Which DATEV product does Max ASP use: Kanzlei Rechnungswesen or Mittelstand Faktura mit Rechnungswesen, and which version?
2. **Who books.** Does Max ASP book in its own DATEV, or does a tax advisor book for Max ASP? Is the installation on Max ASP's own server or with DATEVasp?
3. **DATEVconnect.** Is DATEVconnect already installed? If not, may Max ASP's IT install it (free licence, article 95280)?
4. **Bridge.** May Max ASP's IT run a small Windows service in their network that only makes outgoing HTTPS connections to ARGUS in Azure?
5. **Voucher images today.** Where are invoice images kept today: DATEV DMS, DATEV Unternehmen online, another archive, or nowhere?
6. **Booking flow.** Is it fine if ARGUS bookings land in DATEV as an uncommitted batch (or as booking proposals) that Benjamin commits, or must they be final with no human step? BK-01 says "booked immediately", and a batch is booked but not yet committed.
7. **IBANs.** Are vendor IBANs maintained in the DATEV vendor accounts today? PR-05 and VM-04 depend on that.
8. **Test client.** Can Max ASP create a DATEV test client (a copy or an invented company) we can run tests against, before ARGUS touches the real books?
9. **DATEV developer access.** May we register for DATEV's developer portal in Max ASP's name to get the official DATEVconnect API specification?

## Sources

- [DATEVconnect product page (DATEV shop, article 95280)](https://www.datev.de/web/de/shop/produkt-details/datevconnect-95280)
- [ELO: DATEVconnect setup for DATEV Rechnungswesen (server, port, rights, booking proposals since 13.1)](https://ecmonline.zendesk.com/hc/de/articles/360013689179-ELO-DATEV-Schnittstelle-DATEVconnect-f%C3%BCr-DATEV-Rechnungswesen)
- [datev-php-sdk: list of DATEVconnect desktop and online endpoints](https://github.com/Daniel-Jorg-Schuppelius/datev-php-sdk)
- [klardaten DATEVconnect gateway](https://klardaten.com/en/products/datevconnect-gateway)
- [maesn: integrating with DATEV Rechnungswesen](https://www.maesn.com/blog/how-to-integrate-with-datev-rechnungswesen/)
- [maesn: Unternehmen online or Rechnungswesen](https://www.maesn.com/blog/datev-integration-unternehmen-online-vs-rechnungswesen/)
- [apideck: DATEV API integration guide](https://www.apideck.com/blog/datev-api-integration-guide)
- [DATEV Beleganzeige Drittanbieter (article 63183)](https://www.datev.de/web/de/shop/produkt-details/datev-beleganzeige-drittanbieter-63183)
- [edoc: passing the voucher link to DATEV](https://docs.edoc.de/en/edoc-knowledge-base/v1/how-do-i-set-up-the-transfer-of-the-document-link-from-edoc-invoice-to-datev)
- [DATEV Belegbilderservice setup](https://www.datev.de/web/de/berufsgruppenuebergreifend/mydatev/datenservices/belegbilderservice-rechnungswesen-einrichten)
- [fybu: DATEV Buchungsdatenservice setup](https://fybu.de/blog/datev-buchungsdatenservice/)
- [DATEV format (EXTF) on the DATEV developer portal](https://developer.datev.de/datev/platform/en/dtvf)
