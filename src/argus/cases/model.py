"""The names the database uses for kinds, statuses, reasons and outcomes (ADR 0005).

They must match the enums and checks in the migrations; a test compares them."""

from dataclasses import dataclass
from enum import StrEnum


class CaseKind(StrEnum):
    INCOMING_INVOICE = "incoming_invoice"
    INCOMING_CREDIT_NOTE = "incoming_credit_note"
    OUTGOING_INVOICE = "outgoing_invoice"
    OUTGOING_CREDIT_NOTE = "outgoing_credit_note"
    BANK_TRANSACTION = "bank_transaction"


class CaseStatus(StrEnum):
    """The seven statuses of NF-05, plus closed for cases that end without payment."""

    RECEIVED = "received"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    BOOKED = "booked"
    IN_PAYMENT_RUN = "in_payment_run"
    PAID = "paid"
    EXCEPTION = "exception"
    CLOSED = "closed"


class ClosedReason(StrEnum):
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    NOT_AN_INVOICE = "not_an_invoice"
    WITHDRAWN = "withdrawn"


class ExceptionReason(StrEnum):
    UNCERTAIN_CLASSIFICATION = "uncertain_classification"
    UNREADABLE_DOCUMENT = "unreadable_document"
    UNKNOWN_VENDOR = "unknown_vendor"
    UNKNOWN_CUSTOMER = "unknown_customer"
    VENDOR_WITHOUT_APPROVER = "vendor_without_approver"
    DUPLICATE = "duplicate"
    IBAN_MISMATCH = "iban_mismatch"
    INVOICE_CHECK_FAILED = "invoice_check_failed"
    BOOKING_ERROR = "booking_error"
    UNMATCHED_BANK_TRANSACTION = "unmatched_bank_transaction"
    BANK_BALANCE_DIFFERENCE = "bank_balance_difference"
    NUMBER_SEQUENCE_GAP = "number_sequence_gap"
    PAYMENT_FAILED = "payment_failed"
    CUSTOMER_QUERY = "customer_query"
    FORWARD_FAILED = "forward_failed"  # a payment reminder could not be forwarded (#27)
    # The nightly copy of the audit log, or the check against the copies, failed (#83).
    # These two belong to no case, email or bank statement.
    AUDIT_COPY_FAILED = "audit_copy_failed"
    AUDIT_CHECK_FAILED = "audit_check_failed"
    # The invoice number, gross amount or currency is missing or not confirmed, so the case
    # cannot be checked for duplicates. A person corrects the values (#112).
    VALUES_TO_CHECK = "values_to_check"
    # ARGUS cannot propose a G/L account and tax key from the vendor's booking history, and
    # does not guess. A person enters them on the queue page (#37).
    NO_ACCOUNT_PROPOSAL = "no_account_proposal"
    # The vendor's approver is marked away and the vendor has no deputy, so nobody can be asked
    # to approve. ARGUS carries on by itself once someone can (#191).
    APPROVER_ABSENT = "approver_absent"


class Outcome(StrEnum):
    FIXED_IN_DATEV = "fixed_in_datev"
    DATA_CORRECTED = "data_corrected"
    RETRIED = "retried"
    CONFIRMED_DUPLICATE = "confirmed_duplicate"
    NOT_A_DUPLICATE = "not_a_duplicate"
    REJECTED = "rejected"
    ACCEPTED_WITH_COMMENT = "accepted_with_comment"
    ANSWERED = "answered"
    RULE_SAVED = "rule_saved"
    CATEGORY_CHOSEN = "category_chosen"
    PUT_BACK_IN_POOL = "put_back_in_pool"


@dataclass(frozen=True)
class Actor:
    """Who makes a change: a job (``system:intake``) or a person by Entra object ID
    (``user:<id>``). The database refuses any change without one (ADR 0005 rule 2)."""

    kind: str
    name: str

    def __post_init__(self) -> None:
        if self.kind not in ("system", "user") or not self.name.strip():
            raise ValueError("an actor is 'system' or 'user' with a non empty name")

    @classmethod
    def system(cls, job: str) -> "Actor":
        return cls("system", job)

    @classmethod
    def user(cls, entra_object_id: str) -> "Actor":
        return cls("user", entra_object_id)

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"
