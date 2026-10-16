"""Scenario data for the ARGUS synthetic test fixtures.

Everything in this file is invented. Company names, people, addresses, VAT IDs
and IBANs follow the synthetic patterns described in tests/fixtures/README.md,
so none of them can belong to a real person or company.

generate.py turns this data into emails, PDFs, scans, e-invoices and CSV files.
"""

from decimal import Decimal, ROUND_HALF_UP


def iban(country, bban):
    """Build an IBAN with a valid check sum from a synthetic BBAN."""
    rearranged = bban + country + "00"
    digits = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{bban}"


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Max ASP GmbH is the company ARGUS works for. Its name is needed so ARGUS can
# tell incoming from outgoing invoices. Address, VAT ID, IBAN and email domain
# are placeholders.
MAX_ASP = {
    "name": "Max ASP GmbH",
    "street": "Testallee 1",
    "postcode": "70000",
    "city": "Musterstadt",
    "country": "DE",
    "vat_id": "DE099000001",
    "iban": iban("DE", "00000001" + "0000990001"),
    "email": "rechnungen@maxasp.example",
}

ACCOUNTS_MAILBOX = "accounts@maxasp.example"
INVOICING_AGENT = "rechnungen@maxasp.example"

# People at Max ASP. All names are placeholders.
PEOPLE = {
    "second_approver": ("Erika Mustermann", "erika.mustermann@maxasp.example"),
    "anna": ("Anna Muster", "anna.muster@maxasp.example"),
    "jonas": ("Jonas Beispiel", "jonas.beispiel@maxasp.example"),
    "lea": ("Lea Probe", "lea.probe@maxasp.example"),
    "tom": ("Tom Test", "tom.test@maxasp.example"),
}

# Roles from the requirements that point to one person. In the real system
# these are configuration, not code.
ROLES = [
    ("second_approver", "Approves invoices above EUR 5,000 after the vendor's approver (AP-09)", "second_approver"),
    ("reminder_recipient", "Receives forwarded payment reminders and dunning letters (IN-05)", "second_approver"),
    ("escalation_contact", "Receives approvals that are 5 working days overdue (AP-05)", "second_approver"),
    ("exception_owner", "Receives the daily exception digest and exception notices (EX-03, BK-06, AR-03, VM-03)", "second_approver"),
    ("payment_releaser", "Releases payment batches manually in Wise; ARGUS never does (PY-08, NF-04)", "second_approver"),
]

# Vendors known in DATEV (Kreditoren). One vendor has no approver (VM-02).
VENDORS = {
    "muster_buero": {
        "account": "70001", "name": "Muster Bürobedarf GmbH", "street": "Papierweg 12",
        "postcode": "70001", "city": "Musterstadt", "country": "DE",
        "vat_id": "DE012345601", "iban": iban("DE", "00000002" + "0000700001"),
        "email": "rechnungen@muster-buerobedarf.example", "currency": "EUR",
        "terms": "30 Tage netto, 2 % Skonto bei Zahlung innerhalb von 10 Tagen",
        "approver": "anna", "deputy": "lea", "direct_debit": False,
    },
    "cloud": {
        "account": "70002", "name": "Beispiel Cloud Hosting GmbH", "street": "Serverstraße 3",
        "postcode": "70002", "city": "Beispielstadt", "country": "DE",
        "vat_id": "DE012345602", "iban": iban("DE", "00000003" + "0000700002"),
        "email": "billing@beispiel-cloud.example", "currency": "EUR",
        "terms": "Zahlbar innerhalb von 14 Tagen ohne Abzug",
        "approver": "jonas", "deputy": "tom", "direct_debit": False,
    },
    "immobilien": {
        "account": "70003", "name": "Muster Immobilien Verwaltung KG", "street": "Am Musterplatz 7",
        "postcode": "70003", "city": "Musterstadt", "country": "DE",
        "vat_id": "DE012345603", "iban": iban("DE", "00000004" + "0000700003"),
        "email": "verwaltung@muster-immobilien.example", "currency": "EUR",
        "terms": "Einzug per SEPA-Lastschrift zum Monatsersten",
        "approver": "anna", "deputy": "second_approver", "direct_debit": True,
    },
    "software_uk": {
        "account": "70004", "name": "Example Software Ltd", "street": "1 Sample Street",
        "postcode": "XX1 1XX", "city": "Exampleton", "country": "GB",
        "vat_id": "GB000000004", "iban": iban("GB", "TEST" + "000000" + "70000004"),
        "email": "accounts@example-software.example", "currency": "GBP",
        "terms": "Payment due within 30 days",
        "approver": "jonas", "deputy": "tom", "direct_debit": False,
    },
    "beratung": {
        "account": "70005", "name": "Beispiel Beratung Partner mbB", "street": "Ratgeberweg 5",
        "postcode": "70005", "city": "Beispielstadt", "country": "DE",
        "vat_id": "DE012345605", "iban": iban("DE", "00000005" + "0000700005"),
        "email": "office@beispiel-beratung.example", "currency": "EUR",
        "terms": "Zahlbar innerhalb von 14 Tagen ohne Abzug",
        "approver": "lea", "deputy": "anna", "direct_debit": False,
    },
    "telekom": {
        "account": "70006", "name": "Muster Telekom Service GmbH", "street": "Funkturmstraße 9",
        "postcode": "70006", "city": "Musterstadt", "country": "DE",
        "vat_id": "DE012345606", "iban": iban("DE", "00000006" + "0000700006"),
        "email": "rechnung@muster-telekom.example", "currency": "EUR",
        "terms": "Zahlbar innerhalb von 14 Tagen ohne Abzug",
        "approver": "tom", "deputy": "jonas", "direct_debit": False,
    },
    "reinigung": {
        "account": "70007", "name": "Beispiel Reinigung GmbH", "street": "Glanzgasse 2",
        "postcode": "70007", "city": "Beispielstadt", "country": "DE",
        "vat_id": "DE012345607", "iban": iban("DE", "00000007" + "0000700007"),
        "email": "buchhaltung@beispiel-reinigung.example", "currency": "EUR",
        "terms": "Zahlbar innerhalb von 14 Tagen ohne Abzug",
        "approver": "anna", "deputy": "lea", "direct_debit": False,
    },
    "logistik": {
        "account": "70008", "name": "Muster Logistik GmbH", "street": "Paketring 40",
        "postcode": "70008", "city": "Musterstadt", "country": "DE",
        "vat_id": "DE012345608", "iban": iban("DE", "00000008" + "0000700008"),
        "email": "faktura@muster-logistik.example", "currency": "EUR",
        "terms": "Zahlbar innerhalb von 21 Tagen ohne Abzug",
        "approver": None, "deputy": None, "direct_debit": False,
    },
}

# Senders that are not in DATEV. They appear on documents but not in vendors.csv.
UNKNOWN_PARTIES = {
    "druckerei": {
        "name": "Beispiel Druckerei GmbH", "street": "Druckerweg 8", "postcode": "70009",
        "city": "Beispielstadt", "country": "DE", "vat_id": "DE012345609",
        "iban": iban("DE", "00000009" + "0000700009"), "email": "auftrag@beispiel-druckerei.example",
    },
    "catering": {
        "name": "Beispiel Catering GmbH", "street": "Küchenstraße 4", "postcode": "70010",
        "city": "Beispielstadt", "country": "DE", "vat_id": "DE012345610",
        "iban": iban("DE", "00000010" + "0000700010"), "email": "info@beispiel-catering.example",
    },
    "inkasso": {
        "name": "Beispiel Inkasso GmbH", "street": "Forderungsplatz 1", "postcode": "70011",
        "city": "Beispielstadt", "country": "DE", "vat_id": "DE012345611",
        "iban": iban("DE", "00000011" + "0000700011"), "email": "forderung@beispiel-inkasso.example",
    },
    "muster_ag": {
        "name": "Max Muster AG", "street": "Andere Straße 99", "postcode": "70099",
        "city": "Anderstadt", "country": "DE", "vat_id": "DE012345699",
        "iban": None, "email": "info@max-muster-ag.example",
    },
}

# The IBAN Muster Telekom prints on its invoice in E06. It differs from the
# IBAN stored for the vendor, which must block the invoice (PR-05, AC-5).
TELEKOM_CHANGED_IBAN = iban("DE", "00000066" + "0000706666")

# Customers known in DATEV (Debitoren).
CUSTOMERS = {
    "handel": {
        "account": "10001", "name": "Beispiel Handel GmbH", "street": "Marktstraße 10",
        "postcode": "70101", "city": "Beispielstadt", "country": "DE", "vat_id": "DE012345701",
        "email": "einkauf@beispiel-handel.example", "tax_treatment": "standard",
    },
    "retail_nl": {
        "account": "10002", "name": "Example Retail BV", "street": "Voorbeeldstraat 1",
        "postcode": "0000 XX", "city": "Voorbeeldstad", "country": "NL", "vat_id": "NL000000000B01",
        "email": "finance@example-retail.example", "tax_treatment": "reverse_charge_eu",
    },
    "tech_us": {
        "account": "10003", "name": "Example Tech Inc", "street": "100 Sample Avenue",
        "postcode": "00000", "city": "Example City", "country": "US", "vat_id": "",
        "email": "ap@example-tech.example", "tax_treatment": "not_taxable_third_country",
    },
    "stadtwerke": {
        "account": "10004", "name": "Muster Stadtwerke GmbH", "street": "Energieweg 1",
        "postcode": "70104", "city": "Musterstadt", "country": "DE", "vat_id": "DE012345704",
        "email": "rechnungseingang@muster-stadtwerke.example", "tax_treatment": "standard",
    },
}

UNKNOWN_CUSTOMER = {
    "name": "Beispiel Neukunde GmbH", "street": "Startweg 1", "postcode": "70105",
    "city": "Beispielstadt", "country": "DE", "vat_id": "DE012345705",
    "email": "kontakt@beispiel-neukunde.example", "tax_treatment": "standard",
}


def L(description, quantity, unit_price):
    return {"description": description, "quantity": Decimal(str(quantity)), "unit_price": money(unit_price)}


# Invoice and credit note documents. Each one has the fields ARGUS must extract
# (PR-01); generate.py computes the totals and writes them to expected.json.
#   kind: invoice | credit_note | cancellation | proforma
#   direction: incoming | outgoing
#   tax: standard (19 %), reduced (7 %), reverse_charge, not_taxable
#   format: pdf | scan | xrechnung | zugferd | body
INVOICES = [
    {"id": "D01", "direction": "incoming", "kind": "invoice", "issuer": "muster_buero", "number": "BB-2026-1187",
     "date": "2026-10-02", "period": ("2026-09-01", "2026-09-30"), "due": "2026-11-01",
     "discount": {"percent": "2.00", "until": "2026-10-12"}, "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Druckerpapier A4, 500 Blatt", 20, "4.90"), L("Toner schwarz", 2, "64.50"), L("Ordner breit", 30, "2.35")],
     "format": "pdf"},
    {"id": "D02", "direction": "incoming", "kind": "invoice", "issuer": "cloud", "number": "BCH-26-10-0042",
     "date": "2026-10-01", "period": ("2026-10-01", "2026-10-31"), "due": "2026-10-15",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Managed Server L", 1, "189.00"), L("Backup Speicher 500 GB", 1, "24.90")], "format": "pdf"},
    {"id": "D03", "direction": "incoming", "kind": "invoice", "issuer": "immobilien", "number": "MV-2026-11-007",
     "date": "2026-10-05", "period": ("2026-11-01", "2026-11-30"), "due": "2026-11-01",
     "currency": "EUR", "tax": "standard", "lang": "de", "direct_debit": True,
     "lines": [L("Büromiete November 2026", 1, "2450.00"), L("Nebenkostenvorauszahlung November 2026", 1, "380.00")],
     "format": "pdf"},
    {"id": "D04", "direction": "incoming", "kind": "invoice", "issuer": "software_uk", "number": "ESL-INV-10293",
     "date": "2026-10-03", "period": ("2026-10-01", "2027-09-30"), "due": "2026-11-02",
     "currency": "GBP", "tax": "reverse_charge", "lang": "en",
     "lines": [L("Team licence, 12 months, per seat", 10, "39.00")], "format": "pdf"},
    {"id": "D05", "direction": "incoming", "kind": "invoice", "issuer": "beratung", "number": "BBP-2026-0318",
     "date": "2026-10-06", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-20",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Beratung Prozessdigitalisierung (Stunden)", 42, "175.00"), L("Reisekosten pauschal", 1, "150.00")],
     "format": "pdf"},
    {"id": "D06", "direction": "incoming", "kind": "invoice", "issuer": "telekom", "number": "MTS-2026-558201",
     "date": "2026-10-07", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-21",
     "currency": "EUR", "tax": "standard", "lang": "de", "iban_override": TELEKOM_CHANGED_IBAN,
     "lines": [L("Mobilfunk Business Tarif", 8, "29.95"), L("Festnetz und Internet", 1, "89.90")], "format": "pdf"},
    {"id": "D07", "direction": "incoming", "kind": "invoice", "issuer": "reinigung", "number": "R-2026-0931",
     "date": "2026-10-01", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-15",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Unterhaltsreinigung Büro September", 1, "640.00"), L("Fensterreinigung", 1, "180.00")],
     "format": "scan"},
    {"id": "D08", "direction": "incoming", "kind": "invoice", "issuer": "cloud", "number": "BCH-26-10-0057",
     "date": "2026-10-08", "period": ("2026-10-01", "2026-10-31"), "due": "2026-10-22",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Domain Verlängerung", 3, "15.00"), L("SSL Zertifikat", 1, "79.00")], "format": "xrechnung"},
    {"id": "D09", "direction": "incoming", "kind": "invoice", "issuer": "muster_buero", "number": "BB-2026-1243",
     "date": "2026-10-09", "period": ("2026-10-09", "2026-10-09"), "due": "2026-11-08",
     "discount": {"percent": "2.00", "until": "2026-10-19"}, "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Bürostuhl ergonomisch", 2, "289.00")], "format": "zugferd"},
    {"id": "D10", "direction": "incoming", "kind": "credit_note", "issuer": "muster_buero", "number": "GS-BB-2026-0057",
     "date": "2026-10-10", "period": ("2026-10-10", "2026-10-10"), "due": None, "refers_to": "BB-2026-1187",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Rückgabe Ordner breit", 10, "2.35")], "format": "pdf"},
    {"id": "D11", "direction": "incoming", "kind": "credit_note", "issuer": "software_uk", "number": "ESL-CN-00412",
     "date": "2026-10-11", "period": ("2026-10-01", "2027-09-30"), "due": None, "refers_to": "ESL-INV-10293",
     "currency": "GBP", "tax": "reverse_charge", "lang": "en",
     "lines": [L("Licence reduction, 2 seats", 2, "39.00")], "format": "pdf"},
    {"id": "D12", "direction": "incoming", "kind": "invoice", "issuer": "druckerei", "number": "DR-2026-0815",
     "date": "2026-10-08", "period": ("2026-10-08", "2026-10-08"), "due": "2026-10-22",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Flyer DIN A5, 4/4 farbig", 1000, "0.09"), L("Visitenkarten", 500, "0.12")], "format": "pdf"},
    {"id": "D13", "direction": "incoming", "kind": "invoice", "issuer": "logistik", "number": "ML-2026-30412",
     "date": "2026-10-09", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-30",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Paketversand national", 37, "6.90")], "format": "pdf"},
    {"id": "D14", "direction": "incoming", "kind": "invoice", "issuer": "reinigung", "number": "R-2026-0932",
     "date": "2026-10-12", "period": ("2026-10-05", "2026-10-05"), "due": "2026-10-26",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Sonderreinigung Teppichboden", 1, "260.00")],
     "format": "pdf"},
    {"id": "D15", "direction": "incoming", "kind": "invoice", "issuer": "reinigung", "number": "R-2026-0933",
     "date": "2026-10-12", "period": ("2026-10-07", "2026-10-07"), "due": "2026-10-26",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Grundreinigung Küche", 1, "145.00")],
     "format": "pdf"},
    {"id": "D16", "direction": "incoming", "kind": "invoice", "issuer": "reinigung", "number": "R-2026-0934",
     "date": "2026-10-12", "period": ("2026-10-01", "2026-10-31"), "due": "2026-10-26",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Hygieneartikel Nachfüllung", 1, "86.40")],
     "format": "pdf"},
    {"id": "D17", "direction": "incoming", "kind": "invoice", "issuer": "telekom", "number": "MTS-2026-560017",
     "date": "2026-10-13", "period": ("2026-10-01", "2026-10-31"), "due": "2026-10-27",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Zusatzoption Roaming EU", 1, "19.90")],
     "format": "body"},
    # Outgoing invoices issued by Max ASP GmbH. Number MA-2026-0106 is missing on purpose (AR-07).
    {"id": "D21", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "handel",
     "number": "MA-2026-0101", "date": "2026-10-01", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-15",
     "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Hosting und Wartung Webshop September", 1, "1200.00"), L("Supportstunden", 6, "95.00")],
     "format": "pdf"},
    {"id": "D22", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "retail_nl",
     "number": "MA-2026-0102", "date": "2026-10-01", "period": ("2026-09-01", "2026-09-30"), "due": "2026-10-31",
     "currency": "EUR", "tax": "reverse_charge", "lang": "en",
     "lines": [L("Application hosting September", 1, "2400.00")], "format": "pdf"},
    {"id": "D23", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "tech_us",
     "number": "MA-2026-0103", "date": "2026-10-02", "period": ("2026-09-01", "2026-09-30"), "due": "2026-11-01",
     "currency": "USD", "tax": "not_taxable", "lang": "en",
     "lines": [L("Consulting services September (hours)", 16, "150.00")], "format": "pdf"},
    {"id": "D24", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "stadtwerke",
     "number": "MA-2026-0104", "date": "2026-10-05", "period": ("2026-10-01", "2026-10-31"), "due": "2026-11-04",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Portalbetrieb Oktober 2026", 1, "3800.00")],
     "format": "xrechnung"},
    {"id": "D25", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "unknown_customer",
     "number": "MA-2026-0105", "date": "2026-10-07", "period": ("2026-10-07", "2026-10-07"), "due": "2026-10-21",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Einrichtungspauschale Kundenportal", 1, "990.00")],
     "format": "pdf"},
    {"id": "D26", "direction": "outgoing", "kind": "invoice", "issuer": "max_asp", "recipient": "handel",
     "number": "MA-2026-0107", "date": "2026-10-12", "period": ("2026-10-01", "2026-10-12"), "due": "2026-10-26",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Supportstunden Oktober", 4, "95.00")],
     "format": "pdf"},
    {"id": "D27", "direction": "outgoing", "kind": "credit_note", "issuer": "max_asp", "recipient": "handel",
     "number": "GS-2026-0011", "date": "2026-10-09", "period": ("2026-09-01", "2026-09-30"), "due": None,
     "refers_to": "MA-2026-0101", "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Gutschrift Supportstunden, doppelt berechnet", 2, "95.00")], "format": "pdf"},
    {"id": "D28", "direction": "outgoing", "kind": "cancellation", "issuer": "max_asp", "recipient": "stadtwerke",
     "number": "GS-2026-0012", "date": "2026-10-13", "period": ("2026-10-01", "2026-10-31"), "due": None,
     "refers_to": "MA-2026-0104", "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Storno Portalbetrieb Oktober 2026", 1, "3800.00")], "format": "pdf"},
    # Documents for the deliberately ambiguous emails.
    {"id": "D31", "direction": "incoming", "kind": "proforma", "issuer": "software_uk", "number": "PF-2026-0077",
     "date": "2026-10-12", "period": ("2026-11-01", "2027-10-31"), "due": "2026-10-26",
     "currency": "GBP", "tax": "reverse_charge", "lang": "en",
     "lines": [L("Premium support add-on, 12 months", 1, "480.00")], "format": "pdf"},
    {"id": "D32", "direction": "incoming", "kind": "invoice", "issuer": "muster_buero", "number": "BB-2026-1290",
     "date": "2026-10-13", "period": ("2026-10-13", "2026-10-13"), "due": "2026-11-12",
     "discount": {"percent": "2.00", "until": "2026-10-23"}, "currency": "EUR", "tax": "standard", "lang": "de",
     "lines": [L("Kopierpapier A4, 500 Blatt", 10, "4.90")], "format": "pdf"},
    {"id": "D33", "direction": "incoming", "kind": "invoice", "issuer": "catering", "number": "BC-2026-221",
     "date": "2026-10-09", "period": ("2026-10-08", "2026-10-08"), "due": "2026-10-23",
     "currency": "EUR", "tax": "reduced", "lang": "de",
     "lines": [L("Catering Teammeeting, Speisen", 24, "13.00")], "format": "pdf"},
    {"id": "D34", "direction": "incoming", "kind": "invoice", "issuer": "reinigung", "recipient": "muster_ag",
     "number": "R-2026-0950", "date": "2026-10-13", "period": ("2026-10-01", "2026-10-31"), "due": "2026-10-27",
     "currency": "EUR", "tax": "standard", "lang": "de", "lines": [L("Unterhaltsreinigung Oktober", 1, "410.00")],
     "format": "pdf"},
]

# Letters that are not invoices: reminders, quotes, order confirmations and so on.
LETTERS = {
    "L25": {"issuer": "muster_buero", "title": "Zahlungserinnerung", "date": "2026-10-02", "format": "pdf", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "sicher ist es Ihrer Aufmerksamkeit entgangen, dass folgende Rechnung noch offen ist:",
        "Rechnung BB-2026-1101 vom 20.08.2026 über 212,40 EUR, fällig am 19.09.2026.",
        "Bitte überweisen Sie den Betrag innerhalb der nächsten 7 Tage. Sollten Sie bereits gezahlt haben, betrachten Sie dieses Schreiben bitte als gegenstandslos.",
        "Mit freundlichen Grüßen\nMuster Bürobedarf GmbH, Buchhaltung"]},
    "L26": {"issuer": "cloud", "title": "1. Mahnung", "date": "2026-10-06", "format": "pdf", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "trotz Fälligkeit konnten wir für folgende Rechnung keinen Zahlungseingang feststellen:",
        "Rechnung BCH-26-09-0038 vom 01.09.2026 über 254,54 EUR. Zuzüglich Mahngebühr 5,00 EUR. Offener Betrag: 259,54 EUR.",
        "Bitte gleichen Sie den offenen Betrag bis zum 16.10.2026 aus.",
        "Mit freundlichen Grüßen\nBeispiel Cloud Hosting GmbH, Forderungsmanagement"]},
    "L28": {"issuer": "logistik", "title": "Letzte Mahnung", "date": "2026-10-08", "format": "scan", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "auf unsere Zahlungserinnerung und 1. Mahnung haben Sie nicht reagiert.",
        "Rechnung ML-2026-29870 vom 05.08.2026 über 288,90 EUR ist seit dem 26.08.2026 fällig.",
        "Wir fordern Sie letztmalig auf, den Betrag bis zum 18.10.2026 zu überweisen. Danach übergeben wir die Forderung ohne weitere Ankündigung an ein Inkassobüro.",
        "Mit freundlichen Grüßen\nMuster Logistik GmbH"]},
    "L29": {"issuer": "inkasso", "title": "Zahlungsaufforderung", "date": "2026-10-09", "format": "pdf", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "unsere Mandantin, die Beispiel Werbemittel GmbH, hat uns mit dem Einzug folgender Forderung beauftragt:",
        "Rechnung BW-2026-0412 vom 01.07.2026: Hauptforderung 640,00 EUR, Mahnkosten 10,00 EUR, Inkassokosten 70,20 EUR. Gesamt: 720,20 EUR.",
        "Bitte zahlen Sie den Gesamtbetrag bis zum 23.10.2026 unter Angabe des Aktenzeichens IK-26-77310.",
        "Mit freundlichen Grüßen\nBeispiel Inkasso GmbH"]},
    "L31": {"issuer": "reinigung", "title": "2. Mahnung", "date": "2026-10-12", "format": "pdf", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "leider ist folgende Rechnung trotz unserer 1. Mahnung weiterhin offen:",
        "Rechnung R-2026-0811 vom 01.09.2026 über 975,80 EUR, fällig am 15.09.2026.",
        "Wir bitten um Ausgleich bis zum 19.10.2026.",
        "Mit freundlichen Grüßen\nBeispiel Reinigung GmbH"]},
    "L35": {"issuer": "muster_buero", "title": "Herbst-Angebote 2026", "date": "2026-10-05", "format": "pdf", "paragraphs": [
        "Unsere Highlights für Ihr Büro:",
        "Druckerpapier A4, 500 Blatt: jetzt 4,49 EUR statt 4,90 EUR",
        "Bürostuhl ergonomisch: jetzt 259,00 EUR statt 289,00 EUR",
        "Schreibtischlampe LED: jetzt 39,90 EUR",
        "Gültig bis 31.10.2026. Alle Preise zzgl. MwSt."]},
    "L37": {"issuer": "beratung", "title": "Angebot Nr. AN-2026-044", "date": "2026-10-07", "format": "pdf", "paragraphs": [
        "Sehr geehrte Damen und Herren,",
        "vielen Dank für Ihre Anfrage. Gerne bieten wir Ihnen an:",
        "Workshop Prozessanalyse, 2 Tage: 2.800,00 EUR netto",
        "Umsetzungsbegleitung, 20 Stunden à 175,00 EUR: 3.500,00 EUR netto",
        "Angebotssumme netto 6.300,00 EUR, zzgl. 19 % USt 1.197,00 EUR, gesamt 7.497,00 EUR.",
        "Dieses Angebot ist gültig bis zum 06.11.2026.",
        "Mit freundlichen Grüßen\nBeispiel Beratung Partner mbB"]},
    "L38": {"issuer": "muster_buero", "title": "Auftragsbestätigung AB-2026-5531", "date": "2026-10-08", "format": "pdf", "paragraphs": [
        "Vielen Dank für Ihre Bestellung vom 07.10.2026. Wir bestätigen:",
        "2 x Bürostuhl ergonomisch à 289,00 EUR",
        "Voraussichtliche Lieferung: 09.10.2026. Die Rechnung erhalten Sie mit separater Post.",
        "Mit freundlichen Grüßen\nMuster Bürobedarf GmbH"]},
    "L39": {"issuer": "logistik", "title": "Lieferschein LS-2026-88412", "date": "2026-10-09", "format": "pdf", "paragraphs": [
        "Sendung: 2 Packstücke, 34 kg",
        "Inhalt: Bürostuhl ergonomisch (2)",
        "Zugestellt am 09.10.2026, 10:42 Uhr. Empfang bestätigt.",
        "Dieser Lieferschein ist keine Rechnung."]},
    "L46": {"issuer": None, "title": "", "date": "2026-10-12", "format": "faded_scan", "paragraphs": [
        "Rechnung", "Nr. ....", "Betrag ...... EUR", "Bitte überweisen Sie ..."]},
}
