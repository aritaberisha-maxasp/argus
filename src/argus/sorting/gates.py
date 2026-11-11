"""The safety gates of ADR 0006 point 7: situations where ARGUS never decides on its own (#71).

Each gate sends the email to manual review, even when the language model is sure of itself and
our checks would otherwise agree with it. A gate has its own name, so the person in the queue,
the audit log and the tests can tell the gates apart. The reason text says what was found and
where (by attachment position, never by file name, NF-03).

Most gates fire in our own checks before the model is asked (:mod:`argus.sorting.checks`). The
ones that need the invoice data fire after it, when the invoices of an email sent to invoice
processing are read (:mod:`argus.intake.invoices`, :mod:`argus.reading.plausibility`).
"""

from dataclasses import dataclass
from enum import StrEnum


class Gate(StrEnum):
    ADDRESSED_TO_SOMEONE_ELSE = "addressed_to_someone_else"
    OWN_INVOICE_NOT_FROM_AGENT = "own_invoice_not_from_agent"
    EMPLOYEE_FORWARD = "employee_forward"
    PRO_FORMA = "pro_forma"
    UNREADABLE_DOCUMENT = "unreadable_document"
    INVOICE_AND_REMINDER = "invoice_and_reminder"
    QUESTION = "question"
    FAILED_PLAUSIBILITY = "failed_plausibility"
    PDF_DISAGREES_WITH_XML = "pdf_disagrees_with_xml"
    EINVOICE_UNREADABLE = "einvoice_unreadable"
    SENDER_FAILS_AUTHENTICATION = "sender_fails_authentication"
    NO_DOCUMENT = "no_document"


# What the person in the manual review queue reads for each gate.
LABELS: dict[Gate, str] = {
    Gate.ADDRESSED_TO_SOMEONE_ELSE: "invoice addressed to someone else",
    Gate.OWN_INVOICE_NOT_FROM_AGENT: "Max ASP invoice not sent by the invoicing agent",
    Gate.EMPLOYEE_FORWARD: "employee forwarded an invoice",
    Gate.PRO_FORMA: "pro forma invoice",
    Gate.UNREADABLE_DOCUMENT: "document cannot be read",
    Gate.INVOICE_AND_REMINDER: "invoice and payment reminder in one email",
    Gate.QUESTION: "question from a customer or vendor",
    Gate.FAILED_PLAUSIBILITY: "invoice data fails a plausibility check",
    Gate.PDF_DISAGREES_WITH_XML: "PDF copy disagrees with its e-invoice",
    Gate.EINVOICE_UNREADABLE: "e-invoice cannot be read safely",
    Gate.SENDER_FAILS_AUTHENTICATION: "sender fails authentication",
    Gate.NO_DOCUMENT: "mentions an invoice but brings no document",
}


@dataclass(frozen=True)
class GateHit:
    """One gate that fired, with the reason a person reads."""

    gate: Gate
    reason: str


def gate_labels(gates: tuple[str, ...]) -> str:
    """The plain names of the gates, in the order they fired, each once."""
    known = {gate.value: label for gate, label in LABELS.items()}
    return "; ".join(known.get(gate, gate) for gate in dict.fromkeys(gates))
