"""What goes into sorting an email and what comes out (IN-04, IN-07, IN-09).

The input is deliberately small: the parts of an email the sorting needs, with the text of each
attachment already read. The mailbox sweep (#24) and the document readers (#28, #29) fill it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Category(StrEnum):
    """The four categories of IN-04. Each email gets exactly one, or goes to manual review."""

    INCOMING_INVOICE = "incoming_invoice"  # invoices and credit notes from others
    OUTGOING_INVOICE = "outgoing_invoice"  # invoices and credit notes issued by Max ASP
    PAYMENT_REMINDER = "payment_reminder"  # reminders, dunning letters, collection
    OTHER = "other"  # marketing, newsletters, spam, general mail


class Route(StrEnum):
    INVOICE_PROCESSING = "invoice_processing"
    OUTGOING_BOOKING = "outgoing_booking"
    FORWARD_REMINDER = "forward_reminder"
    DELETION_FOLDER = "deletion_folder"
    MANUAL_REVIEW = "manual_review"


ROUTE_FOR: Mapping[Category, Route] = {
    Category.INCOMING_INVOICE: Route.INVOICE_PROCESSING,
    Category.OUTGOING_INVOICE: Route.OUTGOING_BOOKING,
    Category.PAYMENT_REMINDER: Route.FORWARD_REMINDER,
    Category.OTHER: Route.DELETION_FOLDER,
}


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    text: str | None  # what a reader got out of it; None when nothing could be read


@dataclass(frozen=True)
class EmailToSort:
    message_id: str
    sender_address: str
    sender_name: str
    subject: str
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)  # names in lower case
    attachments: tuple[Attachment, ...] = ()


# Evidence goes into the audit log, so it stays short (ADR 0006, NF-03).
MAX_EVIDENCE_ITEMS = 10
MAX_SNIPPET_CHARS = 200
# An IBAN, with or without the spaces of its printed form.
IBAN = re.compile(r"\b([A-Z]{2})(\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?)\b", re.IGNORECASE)


def mask_ibans(text: str) -> str:
    """Keep the country code and the last four characters of every IBAN (PR #79 rule)."""

    def mask(match: re.Match[str]) -> str:
        rest = match[2].replace(" ", "")
        return match[1] + "*" * (len(rest) - 4) + rest[-4:]

    return IBAN.sub(mask, text)


class ModelInvoice(BaseModel):
    """One invoice or credit note the model sees in the email."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    number: str | None
    document: str  # attachment file name, or "body"


class ModelAnswer(BaseModel):
    """The fixed JSON answer the language model must give."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    invoices: tuple[ModelInvoice, ...]
    evidence: tuple[str, ...]  # short quotes from the email the model relied on


@dataclass(frozen=True)
class CheckResult:
    """The deterministic second opinion. ``category`` is None when the checks cannot name one."""

    category: Category | None
    reasons: tuple[str, ...]
    issued_by_us: bool  # a document in the email is issued by Max ASP (AR-02)


@dataclass(frozen=True)
class SortingDecision:
    route: Route
    category: Category | None  # None when the email goes to manual review
    reasons: tuple[str, ...]  # why it went to manual review; empty when routed
    model_category: Category | None
    model_confidence: float | None
    check_category: Category | None
    evidence: tuple[str, ...]

    def audit_details(self) -> dict[str, Any]:
        """What goes into the audit log: the decision and the evidence snippets, never the
        prompt or the full model answer (ADR 0006 point 8)."""
        return {
            "route": self.route.value,
            "category": self.category.value if self.category else None,
            "model_category": self.model_category.value if self.model_category else None,
            "model_confidence": self.model_confidence,
            "check_category": self.check_category.value if self.check_category else None,
            "reasons": list(self.reasons),
            "evidence": [
                mask_ibans(snippet[:MAX_SNIPPET_CHARS])
                for snippet in self.evidence[:MAX_EVIDENCE_ITEMS]
            ],
        }
