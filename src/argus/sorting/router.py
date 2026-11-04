"""Decide where each email goes (IN-04, IN-07, IN-09, AR-02).

ARGUS routes an email on its own only when the model's confidence is above the threshold for
its category and our own checks name the same category. Anything in doubt goes to manual review
with the reason. Invoices go on to invoice processing, outgoing invoices to the outgoing stream.
"""

import logging
import re
from dataclasses import replace

from argus.sorting.checks import check_email, normalise
from argus.sorting.config import SortingConfig
from argus.sorting.language_model import LanguageModel, ModelAnswerInvalid
from argus.sorting.models import (
    ROUTE_FOR,
    Category,
    EmailToSort,
    Route,
    SortingDecision,
)
from argus.sorting.sender import SenderCheck

log = logging.getLogger("argus.sorting")


def sort_email(email: EmailToSort, model: LanguageModel, config: SortingConfig) -> SortingDecision:
    """Sort one email. Raises ``ModelUnavailable`` when the model cannot be reached, so the email
    stays unsorted and is tried again. Never call this inside a ``unit_of_work``: record the
    decision afterwards in a short one (see the note on PR #79).

    The mailbox sweep records it (#91): :func:`argus.intake.records.mark_classified` moves the
    email to the step ``classified`` and the database writes ``email_classified`` with
    :meth:`SortingDecision.audit_details` (migration 0005)."""
    checks = check_email(email, config)
    try:
        answer = model.propose(email)
    except ModelAnswerInvalid as error:
        return _review(
            (f"no usable model answer: {error}",),
            None,
            None,
            checks.category,
            evidence=(),
            sender=checks.sender,
        )

    reasons: list[str] = []
    threshold = config.thresholds.for_category(answer.category)
    if not answer.confidence > threshold:
        reasons.append(
            f"model confidence {answer.confidence:.2f} for {answer.category} "
            f"is not above the threshold {threshold:.2f}"
        )
    if checks.category is None:
        reasons += [f"our checks name no category: {why}" for why in checks.reasons]
    elif checks.category != answer.category:
        reasons.append(f"the model says {answer.category}, our checks say {checks.category}")

    # Max ASP's own documents never go to approval or payment (AR-02), whoever says otherwise.
    own = normalise(config.own_company)
    ours = checks.issued_by_us or any(normalise(i.issuer) == own for i in answer.invoices)
    if answer.category == Category.INCOMING_INVOICE and ours:
        reasons.append(
            f"a document issued by {config.own_company} never goes to approval or payment"
        )

    if reasons:
        decision = _review(
            tuple(reasons),
            answer.category,
            answer.confidence,
            checks.category,
            quotes_only(answer.evidence, email),
            sender=checks.sender,
        )
        decision = replace(decision, model_invoices=len(answer.invoices))
    else:
        decision = SortingDecision(
            route=ROUTE_FOR[answer.category],
            category=answer.category,
            reasons=(),
            model_category=answer.category,
            model_confidence=answer.confidence,
            check_category=checks.category,
            evidence=quotes_only(answer.evidence, email),
            sender=checks.sender,
            model_invoices=len(answer.invoices),
        )
    # No Message-ID in the log: it names the sender's domain. The caller logs its own record ID.
    log.debug("Email sorted: %s", decision.route)
    return decision


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def quotes_only(evidence: tuple[str, ...], email: EmailToSort) -> tuple[str, ...]:
    """Keep only the snippets that really appear in the email. Email content can steer the
    model, so anything else it writes is not evidence and stays out of the audit log."""
    # Only what the sender wrote: not the From line or the attachment headers we add ourselves,
    # which hold the address and the file names (NF-03).
    written = [email.subject, email.body, *(a.text or "" for a in email.attachments)]
    text = "\n".join(_squash(part) for part in written)
    return tuple(snippet for snippet in evidence if _squash(snippet) and _squash(snippet) in text)


def _review(
    reasons: tuple[str, ...],
    model_category: Category | None,
    model_confidence: float | None,
    check_category: Category | None,
    evidence: tuple[str, ...],
    sender: SenderCheck | None,
) -> SortingDecision:
    return SortingDecision(
        route=Route.MANUAL_REVIEW,
        category=None,
        reasons=reasons,
        model_category=model_category,
        model_confidence=model_confidence,
        check_category=check_category,
        evidence=evidence,
        sender=sender,
    )
