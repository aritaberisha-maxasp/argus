"""Classify an email (IN-04), split it into cases (IN-08) and decide when to ask a person (IN-09).

Covers: IN-04, IN-07, IN-08, IN-09, PR-03

How a decision is made:
1. Evidence from the documents, the sender and the wording adds points to the four
   categories of IN-04. In production a language model gives its own category and
   confidence as one more piece of evidence (see the ADR); here the rules stand in for it.
2. Confidence is the share of points the winning category got.
3. Safety gates send the email to manual review whatever the points say. Each gate
   is a business rule that a person should look at, such as an invoice addressed to
   someone else.
4. Deleting (category Other) needs a higher confidence than any other route,
   because a lost invoice is the most expensive mistake ARGUS can make.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from documents import ParsedEmail

CATEGORIES = ("incoming_invoice", "outgoing_invoice", "payment_reminder", "other")
ROUTES = {
    "incoming_invoice": "invoice_processing",
    "outgoing_invoice": "outgoing_booking",
    "payment_reminder": "forward_reminder",
    "other": "deletion_folder",
    "uncertain": "manual_review",
}

# Thresholds. Tuned in a later story on a labelled sample of real mail, not on fixtures.
MIN_CONFIDENCE = 0.75
MIN_CONFIDENCE_TO_DELETE = 0.90
MIN_POINTS = 2


@dataclass
class Config:
    own_company: str = "Max ASP GmbH"
    own_domain: str = "maxasp.example"
    invoicing_agent: str = "rechnungen@maxasp.example"
    vendors: list[str] = field(default_factory=list)
    customers: list[str] = field(default_factory=list)

    @classmethod
    def from_master_data(cls, folder: Path) -> "Config":
        def names(file: str) -> list[str]:
            with (folder / file).open(encoding="utf-8") as f:
                return [row["name"] for row in csv.DictReader(f)]

        return cls(vendors=names("vendors.csv"), customers=names("customers.csv"))


@dataclass
class Case:
    """One processing case per invoice or credit note (IN-08)."""

    source: str
    kind: str
    direction: str
    fields: dict
    problems: list[str]


@dataclass
class Decision:
    category: str  # one of CATEGORIES or "uncertain"
    route: str
    confidence: float
    proposed: str  # what the points alone said
    points: dict[str, float]
    evidence: list[str]
    gates: list[str]
    cases: list[Case]


# ---------------------------------------------------------------- word lists

REMINDER_WORDS = r"zahlungserinnerung|mahnung|mahngebühr|inkasso|zahlungsaufforderung|noch offen|erinnern|overdue|payment reminder|reminder|past due|final notice"
MARKETING_WORDS = r"newsletter|unsubscribe|abbestellen|abmelden|jetzt anmelden|webinar|what's new|neuigkeiten|angebote|rabatt|sale\b|read more on our blog"
INVOICE_WORDS = r"rechnung|invoice|gutschrift|credit note|betrag|zahlung|payment|€|\beur\b"
AUTO_REPLY_SUBJECT = r"^(automatische antwort|auto(matic)?[ -]?reply|out of office|abwesenheit|abwesenheitsnotiz)"
FORWARD_SUBJECT = r"^(wg|fw|fwd|weitergeleitet)\s*:"
LINK = r"https?://"
CLICK_TO_ACT = r"click here|klicken sie|bestätigen sie|confirm your|verify your|view and pay|login"


def _norm(name: str) -> str:
    """Company name without legal form, for loose matching: "Muster Bürobedarf"."""
    name = re.sub(r"\b(gmbh|kg|ag|ltd|bv|inc|mbb|partner|ug|se|co)\b\.?", " ", name.lower())
    return re.sub(r"[^a-zäöüß0-9]+", " ", name).strip()


def _slug(name: str) -> str:
    return _norm(name).replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss").replace(" ", "-")


def sender_kind(mail: ParsedEmail, config: Config) -> str:
    address = mail.sender_address
    if address == config.invoicing_agent:
        return "invoicing_agent"
    if address.endswith("@" + config.own_domain):
        return "internal"
    domain = address.split("@")[-1]
    for kind, names in (("known_vendor", config.vendors), ("known_customer", config.customers)):
        for name in names:
            if _norm(name) and (_norm(name) in _norm(mail.sender_name) or _slug(name) in domain):
                return kind
    return "unknown"


# ---------------------------------------------------------------- decision


def classify(mail: ParsedEmail, config: Config) -> Decision:
    points = dict.fromkeys(CATEGORIES, 0.0)
    evidence: list[str] = []
    gates: list[str] = []

    def add(category: str, value: float, why: str) -> None:
        points[category] += value
        evidence.append(f"{category} +{value:g}: {why}")

    sender = sender_kind(mail, config)
    subject = mail.subject.lower()
    body = mail.body.lower()
    words = subject + "\n" + body
    own = _norm(config.own_company)

    invoices = [d for d in mail.documents if d.is_invoice]
    reminders = [d for d in mail.documents if d.kind == "reminder"]
    not_invoices = [d for d in mail.documents if d.kind in ("quote", "advertising", "order_confirmation", "delivery_note")]

    # Evidence from documents.
    for doc in invoices:
        issuer = _norm(doc.fields.get("issuer_name") or "")
        recipient = _norm(doc.fields.get("recipient_name") or "")
        label = f"{doc.kind} {doc.fields.get('invoice_number') or '?'} in {doc.source}"
        if issuer == own:
            add("outgoing_invoice", 3, f"{label} issued by {config.own_company}")
            if sender != "invoicing_agent":
                gates.append(f"{config.own_company} invoice sent by {mail.sender_address}, not by the invoicing agent")
        else:
            add("incoming_invoice", 3, f"{label} from {doc.fields.get('issuer_name')}")
            if recipient != own:
                gates.append(f"{label} is addressed to {doc.fields.get('recipient_name')!r}, not {config.own_company} (PR-03)")
        if doc.einvoice:
            add("outgoing_invoice" if issuer == own else "incoming_invoice", 1, f"structured e-invoice ({doc.einvoice['syntax'].upper()})")
        if doc.problems:
            gates.append(f"{label}: " + "; ".join(doc.problems))
    if sender == "invoicing_agent":
        add("outgoing_invoice", 2, "sent by the invoicing agent")
    for doc in reminders:
        add("payment_reminder", 3, f"reminder letter in {doc.source}")
    for doc in not_invoices:
        add("other", 3, f"{doc.kind.replace('_', ' ')} in {doc.source}, not an invoice")
    for doc in mail.documents:
        if doc.kind == "proforma":
            gates.append(f"pro forma invoice in {doc.source}: asks for payment but is not a VAT invoice")
        gates.extend(doc.errors)
        if not doc.readable and not doc.errors:
            gates.append(f"{doc.source} cannot be read (OCR confidence {doc.ocr_confidence or 0:.0f})")

    # Evidence from wording.
    if re.search(REMINDER_WORDS, subject):
        add("payment_reminder", 2, "reminder words in the subject")
    elif re.search(REMINDER_WORDS, body):
        add("payment_reminder", 1, "reminder words in the body")
    marketing = set(re.findall(MARKETING_WORDS, words))
    if marketing or "list-unsubscribe" in mail.headers:
        add("other", min(3, 1 + len(marketing)), "marketing or newsletter wording: " + ", ".join(sorted(marketing)))
    if re.search(AUTO_REPLY_SUBJECT, subject) or mail.headers.get("auto-submitted", "no") != "no":
        add("other", 3, "automatic reply")
    if sender == "unknown" and not mail.attachment_count and re.search(LINK, body) and re.search(CLICK_TO_ACT, body):
        add("other", 3, "unknown sender asks to click a link, no document (phishing or spam pattern)")
    if sender == "internal" and not invoices:
        add("other", 2, "internal email without an invoice")
    if sender in ("known_vendor", "known_customer") and not mail.documents and not re.search(INVOICE_WORDS, words):
        add("other", 2, "general notice from a known business partner, no invoice or payment words")

    # Gates from the sender and the situation.
    if sender == "internal" and invoices:
        gates.append("an employee forwarded an invoice: may be an expense claim or already paid")
    if invoices and points["payment_reminder"] > 0:
        gates.append("invoice and payment reminder in one email: two categories apply")
    if sender in ("known_vendor", "known_customer") and not invoices and not reminders and "?" in body and re.search(r"rechnung|invoice", words):
        gates.append(f"question about an invoice from a {sender.replace('_', ' ')}: needs an answer from a person")

    total = sum(points.values())
    proposed = max(points, key=points.get) if total else "other"
    confidence = points[proposed] / total if total else 0.0

    if total < MIN_POINTS:
        gates.append("too little evidence for any category")
    elif confidence < MIN_CONFIDENCE:
        gates.append(f"confidence {confidence:.2f} is below {MIN_CONFIDENCE}")
    elif proposed == "other" and confidence < MIN_CONFIDENCE_TO_DELETE:
        gates.append(f"confidence {confidence:.2f} is too low to delete (needs {MIN_CONFIDENCE_TO_DELETE})")

    category = "uncertain" if gates else proposed
    cases = [
        Case(source=d.source, kind=d.kind, direction="outgoing" if proposed == "outgoing_invoice" else "incoming",
             fields=d.fields, problems=d.problems)
        for d in invoices
    ] if proposed in ("incoming_invoice", "outgoing_invoice") or category == "uncertain" else []
    # A pro forma becomes a case for the reviewer to look at, never an automatic booking.
    cases += [Case(source=d.source, kind=d.kind, direction="incoming", fields=d.fields, problems=[])
              for d in mail.documents if d.kind == "proforma"]

    return Decision(category, ROUTES[category], round(confidence, 2), proposed, points, evidence, gates, cases)
