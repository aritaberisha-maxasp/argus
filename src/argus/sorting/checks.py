"""Our own checks: a second opinion on the category that does not depend on the model (ADR 0006).

The checks name a category only when every sign in the email points to the same one. Mixed or
missing signs give no category, and then the email goes to manual review because the model and
the checks do not agree. The named safety gates of ADR 0006 point 7 are their own story (#71).

These rules come from the document understanding prototype (spikes/document_understanding).
"""

import re
from dataclasses import dataclass

from argus.sorting import sender
from argus.sorting.config import SortingConfig
from argus.sorting.models import Attachment, Category, CheckResult, EmailToSort

# Below this many characters a document counts as unreadable (IN-03).
MIN_READABLE_CHARS = 40

REMINDER_WORDS = re.compile(
    r"zahlungserinnerung|mahnung|mahngebühr|inkasso|zahlungsaufforderung|noch offen|erinnern"
    r"|overdue|payment reminder|reminder|past due|final notice"
)
MARKETING_WORDS = re.compile(
    r"newsletter|unsubscribe|abbestellen|abmelden|jetzt anmelden|webinar|what's new"
    r"|neuigkeiten|angebote|rabatt|\bsale\b|read more on our blog"
)
INVOICE_WORDS = re.compile(r"rechnung|invoice|gutschrift|credit note")
AUTO_REPLY_SUBJECT = re.compile(
    r"^(automatische antwort|auto(matic)?[ -]?reply|out of office|abwesenheit|abwesenheitsnotiz)"
)
LINK = re.compile(r"https?://")
CLICK_TO_ACT = re.compile(
    r"click here|klicken sie|bestätigen sie|confirm your|verify your|view and pay|login"
)

# Document titles, checked on short lines near the top. Order matters: the more specific title
# wins ("Stornorechnung" before "Rechnung").
TITLES = [
    ("proforma", r"pro[\s-]?forma(\s+(invoice|rechnung))?"),
    ("cancellation", r"stornorechnung|storno|cancellation invoice"),
    ("credit_note", r"gutschrift|rechnungskorrektur|credit note"),
    (
        "reminder",
        r"(\d\.\s*)?mahnung|letzte mahnung|zahlungserinnerung|zahlungsaufforderung"
        r"|payment reminder|reminder|final notice",
    ),
    ("quote", r"angebot(\s+nr\.?)?|quotation|quote"),
    ("advertising", r"[\w-]*angebote\s+\d{4}|prospekt|katalog|catalogue"),
    ("order_confirmation", r"auftragsbestätigung|auftragsbestaetigung|order confirmation"),
    ("delivery_note", r"lieferschein|delivery note"),
    ("invoice", r"rechnung|invoice|tax invoice"),
]
_TITLE_RES = [(kind, re.compile(rf"^(?:{pattern})\b", re.IGNORECASE)) for kind, pattern in TITLES]
INVOICE_KINDS = ("invoice", "credit_note", "cancellation")
NOT_INVOICE_KINDS = ("quote", "advertising", "order_confirmation", "delivery_note")
VAT_ID_LINE = re.compile(r"(USt-?IdNr|VAT ID|VAT No)", re.IGNORECASE)


def document_kind(text: str) -> str:
    """The document title: a short line near the top that starts with a known document word."""
    for line in text.splitlines()[:40]:
        line = line.strip()
        if not line or len(line.split()) > 4:
            continue
        for kind, regex in _TITLE_RES:
            if regex.match(line):
                return kind
    return "unknown"


def normalise(name: str) -> str:
    """A company name without its legal form, for loose matching: "muster bürobedarf"."""
    name = re.sub(r"\b(gmbh|kg|ag|ltd|bv|inc|mbb|partner|ug|se|co)\b\.?", " ", name.lower())
    return re.sub(r"[^a-zäöüß0-9]+", " ", name).strip()


@dataclass(frozen=True)
class _Document:
    source: str  # "attachment 2" or "the email text", never a file name (NF-03)
    kind: str
    issuer: str  # normalised
    names_us: bool  # Max ASP is named somewhere other than as the issuer


def _read_document(source: str, text: str, own: str) -> _Document:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # The issuer's name sits above its address line, which sits above its VAT ID.
    vat_line = next((i for i, line in enumerate(lines[:20]) if VAT_ID_LINE.match(line)), None)
    issuer_at = vat_line - 2 if vat_line is not None and vat_line >= 2 else 0
    issuer = normalise(lines[issuer_at]) if lines else ""
    names_us = any(normalise(line) == own for i, line in enumerate(lines) if i != issuer_at)
    return _Document(source, document_kind(text), issuer, names_us)


def claimed_sender(address: str, config: SortingConfig) -> str:
    """Who the From address says the sender is. Only the exact address of the invoicing agent and
    the exact domains of known partners count: a domain that merely looks like one is unknown."""
    address = address.strip().lower()
    domain = sender.domain_of(address) if "@" in address else ""
    if address == config.invoicing_agent.strip().lower():
        return sender.INVOICING_AGENT
    if domain and domain == config.own_domain.strip().lower():
        return sender.INTERNAL
    for kind, domains in (
        (sender.KNOWN_VENDOR, config.vendor_domains),
        (sender.KNOWN_CUSTOMER, config.customer_domains),
    ):
        if domain and domain in {sender.domain_of(d) for d in domains}:
            return kind
    return sender.UNKNOWN


def check_sender(email: EmailToSort, config: SortingConfig) -> sender.SenderCheck:
    """The sender counts as the invoicing agent or a known partner only when the email really
    comes from that domain (ADR 0006 point 4, #70). Otherwise it counts as unknown."""
    claimed = claimed_sender(email.sender_address, config)
    authentication = sender.authenticate(email.sender_address, email.headers)
    if claimed in sender.NEEDS_CHECK and not authentication.passed:
        return sender.SenderCheck(claimed, sender.UNKNOWN, authentication)
    # Mail inside the tenant often carries no DMARC result, so Max ASP's own domain is not
    # checked. But when Exchange says DMARC fails for it, the email came from outside.
    if claimed == sender.INTERNAL and authentication.dmarc_failed:
        return sender.SenderCheck(claimed, sender.UNKNOWN, authentication)
    return sender.SenderCheck(claimed, claimed, authentication)


def _readable(attachment: Attachment) -> bool:
    return attachment.text is not None and len(re.sub(r"\s", "", attachment.text)) >= (
        MIN_READABLE_CHARS
    )


def check_email(email: EmailToSort, config: SortingConfig) -> CheckResult:
    own = normalise(config.own_company)
    who = check_sender(email, config)
    sender_kind = who.treated_as
    subject = email.subject.lower()
    body = email.body.lower()
    words = subject + "\n" + body

    signs: dict[Category, list[str]] = {}
    doubts: list[str] = []
    if who.failed_claim:
        # Always a person, whatever else the email says (ADR 0006 points 4 and 7).
        doubts.append(who.reason())

    def sign(category: Category, why: str) -> None:
        signs.setdefault(category, []).append(why)

    # Reasons name attachments by position: file names can hold personal names and the reasons
    # go into the audit log (NF-03, as for the document store in #20).
    documents: list[_Document] = []
    for position, attachment in enumerate(email.attachments, start=1):
        if _readable(attachment):
            documents.append(_read_document(f"attachment {position}", attachment.text or "", own))
        else:
            doubts.append(f"attachment {position} cannot be read")
    body_document = _read_document("the email text", email.body, own)
    if body_document.kind in INVOICE_KINDS:
        documents.append(body_document)  # an invoice written in the email itself (E16)

    invoices = [d for d in documents if d.kind in INVOICE_KINDS]
    issued_by_us = any(d.issuer == own for d in invoices)
    for doc in invoices:
        if doc.issuer == own:
            if sender_kind == sender.INVOICING_AGENT:
                sign(Category.OUTGOING_INVOICE, f"{doc.source} is issued by {config.own_company}")
            else:
                doubts.append(
                    f"{doc.source} is issued by {config.own_company} but was not sent by the "
                    "invoicing agent"
                )
        elif doc.names_us:
            sign(Category.INCOMING_INVOICE, f"{doc.source} is an invoice to {config.own_company}")
        else:
            doubts.append(f"{doc.source} is an invoice that does not name {config.own_company}")
    for doc in documents:
        if doc.kind == "reminder":
            sign(Category.PAYMENT_REMINDER, f"{doc.source} is a payment reminder")
        elif doc.kind in NOT_INVOICE_KINDS:
            sign(Category.OTHER, f"{doc.source} is a {doc.kind.replace('_', ' ')}, not an invoice")
        elif doc.kind == "proforma":
            doubts.append(f"{doc.source} is a pro forma invoice, not a VAT invoice")

    if REMINDER_WORDS.search(words):
        sign(Category.PAYMENT_REMINDER, "reminder words in the subject or text")
    if MARKETING_WORDS.search(words) or "list-unsubscribe" in email.headers:
        sign(Category.OTHER, "marketing or newsletter wording")
    if AUTO_REPLY_SUBJECT.search(subject) or email.headers.get("auto-submitted", "no") != "no":
        sign(Category.OTHER, "automatic reply")
    if (
        sender_kind == sender.UNKNOWN
        and not email.attachments
        and LINK.search(body)
        and CLICK_TO_ACT.search(body)
    ):
        sign(Category.OTHER, "unknown sender asks to click a link and brings no document")
    if sender_kind == sender.INTERNAL:
        if invoices:
            doubts.append("an employee forwarded an invoice")
        else:
            sign(Category.OTHER, "internal email without an invoice")
    if sender_kind in (sender.KNOWN_VENDOR, sender.KNOWN_CUSTOMER) and not documents:
        if not INVOICE_WORDS.search(words):
            sign(Category.OTHER, "general notice from a business partner, no invoice words")
        elif "?" in body:
            doubts.append("a business partner asks about an invoice")

    if doubts:
        return CheckResult(None, tuple(doubts), issued_by_us, who)
    if len(signs) > 1:
        named = " and ".join(sorted(signs))
        return CheckResult(None, (f"signs for {named} in one email",), issued_by_us, who)
    if not signs:
        return CheckResult(None, ("no clear sign for any category",), issued_by_us, who)
    [(category, why)] = signs.items()
    return CheckResult(category, tuple(why), issued_by_us, who)
