"""Turn the fixture emails into the input of the sorting (IN-04).

This stands in for the mailbox sweep (#24) and the document readers (#28, #29) until they are
built: PDFs are read with pypdf, e-invoice XML is read just enough to name seller and buyer,
and the two readable scans use a recorded reading in ``tests/fixtures/readings``.
"""

import csv
import email
import io
import re
import xml.etree.ElementTree as ET
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

import pypdf

from argus.sorting import (
    Attachment,
    Category,
    EmailToSort,
    Route,
    SortingConfig,
    SortingDecision,
)
from argus.sorting.sender import first_headers

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EMAILS = FIXTURES / "emails"
READINGS = FIXTURES / "readings"
MODEL_ANSWERS = FIXTURES / "model-answers" / "answers.json"
# Max ASP's placeholder VAT ID in the fixtures (tests/fixtures/tools/fixture_data.py).
OWN_VAT_IDS = frozenset({"DE099000001"})

# Type codes of EN 16931: 380 invoice, 381 credit note, 384 corrected invoice.
TITLE_FOR_TYPE_CODE = {"380": "Rechnung", "381": "Gutschrift", "384": "Rechnungskorrektur"}


def fixture_config() -> SortingConfig:
    def domains(kind: str) -> frozenset[str]:
        with (FIXTURES / "master-data" / "sender-domains.csv").open(encoding="utf-8") as f:
            return frozenset(row["domain"] for row in csv.DictReader(f) if row["kind"] == kind)

    return SortingConfig(
        own_domain="maxasp.example",
        invoicing_agent="rechnungen@maxasp.example",
        vendor_domains=domains("vendor"),
        customer_domains=domains("customer"),
    )


def passes_for(address: str) -> str:
    """The ``Authentication-Results`` header Exchange Online writes when SPF, DKIM and DMARC all
    pass for the domain of ``address``, as in the fixture emails (#70)."""
    domain = address.rpartition("@")[2]
    return (
        f"spf=pass (sender IP is 192.0.2.10) smtp.mailfrom={domain}; dkim=pass (signature was "
        f"verified) header.d={domain};dmarc=pass action=none header.from={domain};"
        "compauth=pass reason=100"
    )


def manifest() -> list[dict[str, str]]:
    with (EMAILS / "manifest.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_fixture_email(path: Path) -> EmailToSort:
    return read_raw_email(path.read_bytes())


def read_raw_email(raw: bytes) -> EmailToSort:
    """The same for an email as stored by the sweep, for the acceptance tests (#66)."""
    message = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    name, address = parseaddr(str(message.get("From", "")))
    part = message.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part is not None else ""
    if part is not None and part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    attachments = []
    for p in message.iter_attachments():
        filename, data = p.get_filename() or "attachment", p.get_payload(decode=True)
        assert isinstance(data, bytes)
        text = _read(filename, p.get_content_type(), data)
        refused = text is None and p.get_content_type() in ("application/xml", "text/xml")
        attachments.append(Attachment(filename, p.get_content_type(), text, refused))
    return EmailToSort(
        message_id=str(message.get("Message-ID", "")).strip(),
        sender_address=address.lower(),
        sender_name=name,
        subject=str(message.get("Subject", "")),
        body=body,
        headers=first_headers(message.items()),
        attachments=tuple(attachments),
    )


def _read(filename: str, content_type: str, data: bytes) -> str | None:
    if content_type == "application/pdf":
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if content_type in ("application/xml", "text/xml"):
        return _einvoice_text(data)
    # A scan: the recorded reading, or nothing when the scan cannot be read (E46).
    reading = READINGS / f"{filename}.txt"
    return reading.read_text(encoding="utf-8") if reading.exists() else None


def _einvoice_text(data: bytes) -> str | None:
    if b"<!DOCTYPE" in data.upper():
        return None
    # Only our own fixtures, and a DTD is refused above.
    root = ET.fromstring(data)  # noqa: S314

    def first(*local_names: str) -> str:
        for element in root.iter():
            if element.tag.rpartition("}")[2] in local_names and (element.text or "").strip():
                return element.text.strip()  # type: ignore[union-attr]
        return ""

    def party_name(tag: str) -> str:
        for element in root.iter():
            if element.tag.rpartition("}")[2] == tag:
                for inner in element.iter():
                    local = inner.tag.rpartition("}")[2]
                    if local in ("RegistrationName", "Name") and (inner.text or "").strip():
                        return inner.text.strip()  # type: ignore[union-attr]
        return ""

    seller = party_name("AccountingSupplierParty") or party_name("SellerTradeParty")
    buyer = party_name("AccountingCustomerParty") or party_name("BuyerTradeParty")
    code = first("InvoiceTypeCode", "CreditNoteTypeCode", "TypeCode")
    number = first("ID")
    # Laid out like the PDF copies: seller, its address line, its VAT ID, then the buyer.
    lines = [seller, "(address)", "USt-IdNr. (in the XML)", buyer]
    return "\n".join([*lines, TITLE_FOR_TYPE_CODE.get(code, "Rechnung"), f"Nummer: {number}"])


CATEGORY_FOR_ROUTE = {
    Route.INVOICE_PROCESSING: Category.INCOMING_INVOICE,
    Route.OUTGOING_BOOKING: Category.OUTGOING_INVOICE,
    Route.FORWARD_REMINDER: Category.PAYMENT_REMINDER,
    Route.DELETION_FOLDER: Category.OTHER,
}


def decision_for(route: Route) -> SortingDecision:
    """A sorting decision for ``route``, for tests that need a classified email."""
    category = CATEGORY_FOR_ROUTE.get(route)
    return SortingDecision(
        route=route,
        category=category,
        reasons=() if category else ("the model and our checks disagree",),
        model_category=category or Category.OTHER,
        model_confidence=0.97,
        check_category=category,
        evidence=(),
    )
