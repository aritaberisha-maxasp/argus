"""Read the invoice data of one document: a PDF, an image, a scan or the email body (#29,
IN-02, IN-03, PR-01, NF-03, EX-02).

The order follows ADR 0006:

1. The reader gives the text of every page and, for Document Intelligence, its own fields with
   a confidence each. A field it is not sure about is flagged for a person.
2. Our own rules fill a field the reader missed when the page states it plainly, and flag a
   field where the page says something else than the reader.
3. The language model may propose the fields still missing. A proposal counts only when the
   words it quotes stand on the page it names and our own parsers read the same value out of
   them. So the IBAN, the payee and the amounts never come from the model alone.
4. Anything still missing stays empty and is flagged. Nothing is guessed.

A document that cannot be read at all goes to the exception queue with the reason unreadable
document (:func:`queue_unreadable`). Call the readers outside any ``unit_of_work`` and record the
result in a short one afterwards (PR #79 review).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from argus.cases.model import ExceptionReason
from argus.cases.store import open_exception
from argus.reading.fields import (
    FIELDS,
    OPTIONAL_FIELDS,
    OPTIONAL_ON_CREDIT_NOTES,
    PROTECTED_FIELDS,
    Flag,
    InvoiceReading,
    ReadField,
    Source,
)
from argus.reading.language_model import MODEL_FIELDS, FieldModel
from argus.reading.pages import Analysis, DocumentNotReadable, DocumentReader, Page
from argus.reading.text_rules import (
    CREDIT_KINDS,
    INVOICE_KINDS,
    confirm,
    document_kind,
    find_fields,
    iban_valid,
    ibans,
    invoice_numbers,
    on_page,
    vat_rates,
)
from argus.sorting.language_model import ModelAnswerInvalid

log = logging.getLogger("argus.reading")

# Documents whose fields ARGUS needs. A pro forma is read too, so a person sees what it says.
READ_KINDS = INVOICE_KINDS | {"proforma", "unknown"}
# Our rules and the reader must agree on these when both read them.
COMPARED_FIELDS = frozenset({"invoice_number", "iban_on_document"}) | PROTECTED_FIELDS - {
    "issuer_name"
}


@dataclass(frozen=True)
class ReadingSettings:
    """How sure a reader must be. The defaults are a careful start; the shadow run (#72)
    sets the real values."""

    flag_below: float = 0.80  # a field below this confidence is kept but flagged for a person
    ignore_below: float = 0.50  # a field below this confidence counts as not read
    min_text_confidence: float = 0.60  # a scan whose words are read less surely is unreadable
    min_readable_chars: int = 40  # a document with less text than this is unreadable

    def __post_init__(self) -> None:
        if not 0.0 <= self.ignore_below <= self.flag_below <= 1.0:
            raise ValueError("ignore_below must not be above flag_below, both between 0 and 1")


def sniff_type(data: bytes) -> str | None:
    """The document type from its first bytes. The content type an email states is not
    trusted, because the sender chose it."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return None


def read_attachment(
    position: int,
    data: bytes,
    reader: DocumentReader,
    model: FieldModel | None = None,
    settings: ReadingSettings | None = None,
) -> InvoiceReading | None:
    """Read attachment number ``position`` (from 1). Returns None for a file that is not a PDF
    or an image, such as e-invoice XML (#28). Raises ``ReaderUnavailable`` or
    ``ModelUnavailable`` when a service cannot be reached, so the email is tried again later."""
    document = f"attachment {position}"
    content_type = sniff_type(data)
    if content_type is None:
        return None
    try:
        analysis = reader.read(data, content_type)
    except DocumentNotReadable as error:
        return _unreadable(document, reader.name, str(error))
    return build_reading(document, reader.name, analysis, model, settings or ReadingSettings())


def read_body(
    body: str, model: FieldModel | None = None, settings: ReadingSettings | None = None
) -> InvoiceReading | None:
    """Read an invoice written in the email body (IN-02, E16). Returns None when the body is not
    an invoice, credit note or cancellation."""
    if document_kind(body) not in INVOICE_KINDS:
        return None
    analysis = Analysis(pages=(Page(1, body),))
    return build_reading("body", "email_body", analysis, model, settings or ReadingSettings())


def _unreadable(document: str, reader: str, why: str, pages: int = 0) -> InvoiceReading:
    log.info("The %s could not be read", document)
    return InvoiceReading(
        document=document, kind="unknown", reader=reader, pages=pages, text="", unreadable=why
    )


def build_reading(
    document: str,
    reader: str,
    analysis: Analysis,
    model: FieldModel | None,
    settings: ReadingSettings,
) -> InvoiceReading:
    whole_text = analysis.text
    readable = len("".join(whole_text.split()))
    if readable < settings.min_readable_chars:
        return _unreadable(
            document, reader, f"only {readable} readable characters", len(analysis.pages)
        )
    if (
        analysis.text_confidence is not None
        and analysis.text_confidence < settings.min_text_confidence
    ):
        return _unreadable(
            document,
            reader,
            f"the words are read with confidence {analysis.text_confidence:.2f} only",
            len(analysis.pages),
        )

    kind = document_kind(whole_text)
    fields = _from_reader(analysis, settings)
    _add_page_text(fields, analysis.pages)

    problems: list[str] = []
    numbers = invoice_numbers(analysis.pages)
    count = max(len(numbers), analysis.invoice_count or 0)
    if count > 1:
        # Two invoices in one file must never become one case silently (spike H04, IN-08).
        problems.append(f"the file holds {count} invoices")
    stated = ibans(analysis.pages)
    if len(stated) > 1:
        # Which IBAN is paid is not ARGUS's to choose, whoever read it.
        fields["iban_on_document"] = ReadField(
            "iban_on_document",
            flag=Flag.SEVERAL_VALUES,
            note=f"the document states {len(stated)} IBANs: {', '.join(stated)}",
        )
    rates = vat_rates(analysis.pages)
    if len(rates) > 1 and fields.get("vat_rate", ReadField("vat_rate")).source is Source.PAGE_TEXT:
        fields["vat_rate"] = ReadField(
            "vat_rate",
            flag=Flag.SEVERAL_VALUES,
            note=f"the document states {len(rates)} VAT rates: {', '.join(rates)}",
        )

    needed = _needed(kind)
    missing = [name for name in needed if name not in fields]
    if model is not None and any(name in MODEL_FIELDS for name in missing):
        try:
            _add_from_model(fields, analysis.pages, missing, model)
        except ModelAnswerInvalid as error:
            problems.append(f"the language model gave no usable answer: {error}")
    for name in needed:
        if name not in fields:
            fields[name] = ReadField(name, flag=Flag.NOT_FOUND, note="not found on the document")

    ordered = {name: fields[name] for name in FIELDS if name in fields}
    return InvoiceReading(
        document=document,
        kind=kind,
        reader=reader,
        pages=len(analysis.pages),
        text=whole_text,
        fields=ordered,
        problems=tuple(problems),
    )


def _needed(kind: str) -> list[str]:
    """The fields that must be there: none for a letter that is not an invoice."""
    if kind not in READ_KINDS:
        return []
    optional = OPTIONAL_FIELDS | (OPTIONAL_ON_CREDIT_NOTES if kind in CREDIT_KINDS else set())
    return [name for name in FIELDS if name not in optional]


def _from_reader(analysis: Analysis, settings: ReadingSettings) -> dict[str, ReadField]:
    fields: dict[str, ReadField] = {}
    for name, found in analysis.fields.items():
        confidence = found.confidence
        if found.value is None or confidence is None or confidence >= settings.flag_below:
            fields[name] = found
        elif confidence >= settings.ignore_below:
            fields[name] = ReadField(
                name,
                found.value,
                found.source,
                confidence,
                found.page,
                found.words,
                Flag.LOW_CONFIDENCE,
                f"read with confidence {confidence:.2f}, check it on the document",
            )
        # Below ignore_below the value counts as not read; the page text or a person fills it.
    return fields


def _add_page_text(fields: dict[str, ReadField], pages: tuple[Page, ...]) -> None:
    for name, found in find_fields(pages).items():
        current = fields.get(name)
        if current is None:
            bad_iban = name == "iban_on_document" and not iban_valid(found.value)
            fields[name] = ReadField(
                name,
                found.value,
                Source.PAGE_TEXT,
                page=found.page,
                words=found.words,
                flag=Flag.FAILED_CHECK if bad_iban else None,
                note="the IBAN check digits are wrong" if bad_iban else None,
            )
        elif (
            name in COMPARED_FIELDS
            and current.value is not None
            and current.value != found.value
            and current.flag is None
        ):
            # The person approving sees the page, so a reader that says something else than
            # the page must be checked (ADR 0006 point 1, the same reasoning as PDF and XML).
            fields[name] = ReadField(
                name,
                current.value,
                current.source,
                current.confidence,
                current.page,
                current.words,
                Flag.READERS_DISAGREE,
                f"the reader says {current.value}, the page text says {found.value}",
            )


def _add_from_model(
    fields: dict[str, ReadField], pages: tuple[Page, ...], missing: list[str], model: FieldModel
) -> None:
    by_number = {page.number: page for page in pages}
    wanted = [name for name in missing if name in MODEL_FIELDS]
    for proposal in model.find_fields(pages, wanted):
        name = proposal.name
        if name not in wanted or (name in fields and fields[name].value is not None):
            continue
        page = by_number.get(proposal.page)
        value: Any = None
        if page is not None and on_page(proposal.words, page):
            value = confirm(name, proposal.value, proposal.words)
        if value is None:
            fields[name] = ReadField(
                name,
                flag=Flag.MODEL_NOT_CONFIRMED,
                note=(
                    f"the language model proposed a value, but page {proposal.page} does not "
                    "say it in the words it quoted"
                ),
            )
            continue
        # Our rules confirmed the words, but the model chose them. Where money goes and how much
        # is confirmed by a person (ADR 0006 point 3, QA on PR #94).
        protected = name in PROTECTED_FIELDS
        fields[name] = ReadField(
            name,
            value,
            Source.LANGUAGE_MODEL,
            page=proposal.page,
            words=proposal.words,
            flag=Flag.FROM_MODEL if protected else None,
            note="only the language model located this value, a person confirms it"
            if protected
            else None,
        )


def queue_unreadable(
    connection: Connection, email_id: int, readings: Sequence[InvoiceReading]
) -> int | None:
    """Put the unreadable documents of one email in the exception queue with the reason
    unreadable document (EX-01, EX-02), as one exception that names every one of them. Run inside
    a ``unit_of_work`` with all readings of the email. Returns the exception ID, or None when every
    document was readable. Safe to repeat: an email has at most one open exception for this."""
    unreadable = [r for r in readings if r.unreadable is not None]
    if not unreadable:
        return None
    existing = connection.execute(
        text(
            "select id from exception_item where email_id = :email_id"
            " and reason = :reason and resolved_at is null"
        ),
        {"email_id": email_id, "reason": ExceptionReason.UNREADABLE_DOCUMENT.value},
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)
    detail = " ".join(f"The {r.document} could not be read: {r.unreadable}." for r in unreadable)
    return open_exception(
        connection,
        ExceptionReason.UNREADABLE_DOCUMENT,
        detail,
        email_id=email_id,
        # The audit log gets the positions and the readers, never the content (NF-03).
        facts={
            "documents": [r.document for r in unreadable],
            "readers": sorted({r.reader for r in unreadable}),
        },
    )
