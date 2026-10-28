"""The invoice data ARGUS reads, field by field, with where each value came from (PR-01).

Every field says who found it, how sure that reader was, and on which page and in which words it
stands. A field ARGUS could not read stays empty and carries a flag, so a person fills it in;
ARGUS never guesses (#29).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# The PR-01 fields, named as in tests/fixtures/documents/expected.json.
PR01_FIELDS = (
    "issuer_name",
    "vendor_vat_id",
    "invoice_number",
    "invoice_date",
    "service_period",
    "net_amount",
    "vat_rate",
    "vat_amount",
    "gross_amount",
    "currency",
    "due_date",
    "early_payment_discount",
    "iban_on_document",
)
# Read as well, because the §14 UStG check (#32) and AR-02 need to know who the invoice is for.
FIELDS = (*PR01_FIELDS, "recipient_name")

# Where money goes and how much. Email content comes from strangers and can steer the language
# model, so these never come from the model alone (ADR 0006 point 3).
PROTECTED_FIELDS = frozenset(
    {"issuer_name", "iban_on_document", "net_amount", "vat_amount", "gross_amount"}
)

# Fields an invoice may simply not have. Left empty without a flag when they are not there.
OPTIONAL_FIELDS = frozenset({"early_payment_discount"})
# A credit note or a cancellation asks for no payment, so it has no due date either.
OPTIONAL_ON_CREDIT_NOTES = frozenset({"due_date"})


class Source(StrEnum):
    EINVOICE_XML = "einvoice_xml"  # #28: the XML is the legal original
    DOCUMENT_INTELLIGENCE = "document_intelligence"  # Azure AI Document Intelligence
    PAGE_TEXT = "page_text"  # our own rules found it in the text of the page
    LANGUAGE_MODEL = "language_model"  # the model found it and our checks confirmed the words


class Flag(StrEnum):
    """Why a person must look at a field."""

    NOT_FOUND = "not_found"
    LOW_CONFIDENCE = "low_confidence"
    READERS_DISAGREE = "readers_disagree"
    MODEL_NOT_CONFIRMED = "model_not_confirmed"
    SEVERAL_VALUES = "several_values"


@dataclass(frozen=True)
class ReadField:
    """One field. ``value`` uses the formats of ``expected.json``: amounts as ``"297.50"``,
    dates as ``"2026-10-02"``, the VAT rate as ``"19"``, IBANs without spaces."""

    name: str
    value: Any = None
    source: Source | None = None
    confidence: float | None = None  # the reader's own confidence from 0 to 1, when it gives one
    page: int | None = None
    words: str | None = None  # the text on the page the value was read from
    flag: Flag | None = None
    note: str | None = None  # what the flag means for this field, for the person who checks

    def __post_init__(self) -> None:
        if self.name not in FIELDS:
            raise ValueError(f"unknown field {self.name}")
        if (self.value is None) != (self.source is None):
            raise ValueError("a field has a value exactly when it has a source")
        if self.source is Source.LANGUAGE_MODEL and (self.page is None or not self.words):
            raise ValueError("a field from the language model must name its page and words")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("a confidence is between 0 and 1")

    def as_record(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source.value if self.source else None,
            "confidence": self.confidence,
            "page": self.page,
            "words": self.words,
            "flag": self.flag.value if self.flag else None,
            "note": self.note,
        }


@dataclass(frozen=True)
class InvoiceReading:
    """What ARGUS read from one document: an attachment, or the email body (IN-02, IN-03).

    ``document`` names the document by position ("attachment 2" or "body"), never by file name,
    because file names can hold personal names (see docs/design/email-sorting.md).
    """

    document: str
    kind: str  # invoice, credit_note, cancellation, proforma, reminder, ..., or unknown
    reader: str  # which reader read the pages
    pages: int
    text: str
    fields: Mapping[str, ReadField] = field(default_factory=dict)
    unreadable: str | None = None  # why the document could not be read; None when it could
    problems: tuple[str, ...] = ()  # about the document as a whole, for the person who checks

    @property
    def flagged(self) -> dict[str, ReadField]:
        return {name: f for name, f in self.fields.items() if f.flag is not None}

    def value(self, name: str) -> Any:
        found = self.fields.get(name)
        return found.value if found else None

    def as_record(self) -> dict[str, Any]:
        """Every field with its value, source, confidence, page and words, ready to store."""
        return {
            "document": self.document,
            "kind": self.kind,
            "reader": self.reader,
            "pages": self.pages,
            "unreadable": self.unreadable,
            "problems": list(self.problems),
            "fields": {name: f.as_record() for name, f in self.fields.items()},
        }
