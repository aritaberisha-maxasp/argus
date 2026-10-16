"""Check the ARGUS synthetic test fixtures for consistency and for real data.

Usage, from the repository root:
    python3 tests/fixtures/tools/check.py

Exits with 1 and lists every problem it finds. Needs pypdf (see requirements.txt).
"""

import csv
import email
import io
import json
import re
import sys
from collections import Counter
from decimal import Decimal
from email import policy
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = {
    "incoming_invoice": "invoice_processing",
    "outgoing_invoice": "outgoing_booking",
    "payment_reminder": "forward_reminder",
    "other": "deletion_folder",
    "uncertain": "manual_review",
}
REQUIREMENT_ID = re.compile(r"^(IN|PR|AP|BK|PY|AR|BA|VM|EX|NF)-\d{2}$")
# Synthetic identifier patterns, see README.md
SYNTHETIC_IBAN = re.compile(r"^(DE\d{2}0000\d{14}|GB\d{2}TEST000000\d{8})$")
SYNTHETIC_VAT_ID = re.compile(r"^(DE0\d{8}|GB000\d{6}|NL000000000B\d{2})$")
ALLOWED_DOMAIN = re.compile(r"@[\w.-]+\.(example|invalid)$")
UBL = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
       "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}
CII = {"ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}

problems = []


def problem(text):
    problems.append(text)


def iban_valid(value):
    rearranged = value[4:] + value[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check_identifiers(where, text):
    for candidate in re.findall(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b", text):
        value = candidate.replace(" ", "")
        if not SYNTHETIC_IBAN.match(value):
            problem(f"{where}: IBAN {value} does not follow the synthetic pattern")
        elif not iban_valid(value):
            problem(f"{where}: IBAN {value} has a wrong check sum")
    for value in re.findall(r"\b(?:DE\d{9}|GB\d{9}|NL\d{9}B\d{2})\b", text):
        if not SYNTHETIC_VAT_ID.match(value):
            problem(f"{where}: VAT ID {value} does not follow the synthetic pattern")
    for address in re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]+", text):
        if not ALLOWED_DOMAIN.search(address):
            problem(f"{where}: email address {address} is not on a reserved .example or .invalid domain")


def pdf_text(data):
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def main():
    manifest = read_csv(ROOT / "emails" / "manifest.csv")
    expected = {d["document_id"]: d for d in json.loads((ROOT / "documents" / "expected.json").read_text("utf-8"))}
    vendors = read_csv(ROOT / "master-data" / "vendors.csv")
    customers = read_csv(ROOT / "master-data" / "customers.csv")
    approvers = read_csv(ROOT / "master-data" / "approvers.csv")

    # AC-1 needs 50 emails covering every IN-04 category plus ambiguous ones.
    if len(manifest) != 50:
        problem(f"manifest has {len(manifest)} emails, AC-1 needs 50")
    counts = Counter(row["expected_category"] for row in manifest)
    for category in CATEGORIES:
        if counts[category] < 3:
            problem(f"only {counts[category]} emails in category {category}")
    if not any(int(row["invoice_count"]) > 1 for row in manifest):
        problem("no email with several invoices (IN-08)")
    ids = [row["email_id"] for row in manifest]
    if len(set(ids)) != len(ids):
        problem("duplicate email IDs in manifest")

    for row in manifest:
        where = row["email_id"]
        if CATEGORIES.get(row["expected_category"]) != row["expected_route"]:
            problem(f"{where}: category {row['expected_category']} does not match route {row['expected_route']}")
        for req in row["requirements"].split():
            if not REQUIREMENT_ID.match(req):
                problem(f"{where}: {req} is not a requirement ID")
        for doc_id in row["documents"].split():
            if doc_id not in expected:
                problem(f"{where}: document {doc_id} missing from expected.json")
        path = ROOT / "emails" / row["file"]
        if not path.exists():
            problem(f"{where}: file {row['file']} missing")
            continue
        raw = path.read_bytes()
        msg = email.message_from_bytes(raw, policy=policy.default)
        for header in ("From", "To", "Subject", "Date", "Message-ID"):
            if header not in msg:
                problem(f"{where}: header {header} missing")
        text = "\n".join(str(msg[h]) for h in ("From", "To")) + "\n"
        body = msg.get_body(preferencelist=("plain", "html"))
        text += body.get_content() if body else ""
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True)
            name = part.get_filename()
            if name.endswith(".pdf"):
                text += pdf_text(data)
            elif name.endswith(".xml"):
                text += data.decode("utf-8")
        check_identifiers(where, text)

    # Every document's numbers add up and match its e-invoice XML.
    for doc_id, doc in expected.items():
        lines = sum(Decimal(line["amount"]) for line in doc["lines"])
        if lines != Decimal(doc["net_amount"]):
            problem(f"{doc_id}: lines add up to {lines}, net amount is {doc['net_amount']}")
        if Decimal(doc["net_amount"]) + Decimal(doc["vat_amount"]) != Decimal(doc["gross_amount"]):
            problem(f"{doc_id}: net plus VAT is not the gross amount")
        for line in doc["lines"]:
            if (Decimal(line["quantity"]) * Decimal(line["unit_price"])).quantize(Decimal("0.01")) != Decimal(line["amount"]):
                problem(f"{doc_id}: line {line['description']} does not add up")
        for name in doc["files"]:
            path = ROOT / "documents" / name
            if not path.exists():
                problem(f"{doc_id}: file {name} missing")
                continue
            if name.endswith("_xrechnung.xml"):
                root = ElementTree.parse(path).getroot()
                payable = root.findtext("cac:LegalMonetaryTotal/cbc:PayableAmount", namespaces=UBL)
                number = root.findtext("cbc:ID", namespaces=UBL)
                if payable != doc["gross_amount"] or number != doc["invoice_number"]:
                    problem(f"{doc_id}: XRechnung says {number} {payable}, expected.json says "
                            f"{doc['invoice_number']} {doc['gross_amount']}")
            if name.endswith("_zugferd.pdf"):
                reader = PdfReader(path)
                attachments = reader.attachments
                if "factur-x.xml" not in attachments:
                    problem(f"{doc_id}: ZUGFeRD PDF has no embedded factur-x.xml")
                    continue
                root = ElementTree.fromstring(attachments["factur-x.xml"][0])
                grand = root.find(".//ram:GrandTotalAmount", CII).text
                if grand != doc["gross_amount"]:
                    problem(f"{doc_id}: factur-x.xml total {grand}, expected.json says {doc['gross_amount']}")
                if "/AF" not in reader.trailer["/Root"]:
                    problem(f"{doc_id}: ZUGFeRD PDF has no /AF entry")

    # Master data: vendor IBANs are valid, and the PR-05 case really differs.
    vendor_ibans = {v["name"]: v["iban"] for v in vendors}
    for v in vendors:
        check_identifiers(f"vendors.csv {v['vendor_account']}", f"{v['iban']} {v['vat_id']}")
    for c in customers:
        check_identifiers(f"customers.csv {c['customer_account']}", c["vat_id"])
    for a in approvers:
        check_identifiers(f"approvers.csv {a['vendor_account']}", f"{a['approver_email']} {a['deputy_email']}")
    mismatches = [d for d in expected.values() if d["direction"] == "incoming" and d["issuer_name"] in vendor_ibans
                  and d["iban_on_document"] != vendor_ibans[d["issuer_name"]]]
    if len(mismatches) != 1:
        problem(f"expected exactly one invoice whose IBAN differs from the vendor (PR-05), found {len(mismatches)}")
    approver_accounts = {a["vendor_account"] for a in approvers}
    if all(v["vendor_account"] in approver_accounts for v in vendors):
        problem("no vendor without an approver (VM-02)")

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"OK: {len(manifest)} emails, {len(expected)} documents, {len(vendors)} vendors, "
          f"{len(customers)} customers, {len(approvers)} approver rows. Categories: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
