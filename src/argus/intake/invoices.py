"""One case per invoice in an email (#30, IN-08, IN-09, NF-01).

Suppliers sometimes send several invoices in one email, as separate attachments or as several
invoices in one PDF. When the sorting sends an email to invoice processing, the sweep reads every
invoice in it and opens one case for each, all pointing back to the same email, each linked to
its own document and with its own reading:

1. :func:`find_invoices` reads each attachment, splitting a file page by page where a page
   starts a new invoice number (:mod:`argus.reading.split`), and the email text. An e-invoice and
   its PDF copy are one invoice (:func:`argus.einvoice.pair`).
2. :func:`doubts_about` compares what it found with the number of invoices the language model
   listed. When they differ, or a file cannot be split safely, or an invoice has no number or
   issuer ARGUS can read, the email goes to manual review instead (IN-09). Nothing is guessed.
3. :func:`open_cases` opens the cases inside the unit of work that records the decision, so
   sorting the same email twice never opens a case twice.

Each case is keyed on its issuer plus its invoice number (:func:`case_key`): two vendors that
both use number 1001 stay two cases. Duplicates across emails are #31's.

Only incoming invoices and credit notes get cases here; outgoing invoices are #52's.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from email import message_from_bytes, policy
from email.message import EmailMessage

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind
from argus.cases.money import to_cents
from argus.cases.store import create_case
from argus.documents.email import attachments_of
from argus.documents.records import link_document_to_case
from argus.einvoice import pair, read_einvoices
from argus.intake.email_text import body_of
from argus.reading.fields import InvoiceReading
from argus.reading.invoice import ReadingSettings, read_attachment_invoices, read_body
from argus.reading.language_model import FieldModel
from argus.reading.pages import DocumentReader
from argus.reading.text_rules import CREDIT_KINDS, INVOICE_KINDS, clean_vat_id
from argus.sorting.checks import normalise
from argus.sorting.models import Route, SortingDecision


@dataclass(frozen=True)
class Invoicing:
    """What the sweep needs to read the invoices of an email: the document reader of #29
    (Document Intelligence in production), and the language model that may fill missing fields."""

    reader: DocumentReader
    # Max ASP's own VAT IDs. An incoming invoice may print them as the customer's, above the
    # supplier's; they are never taken as the issuer's (review of PR 105).
    own_vat_ids: frozenset[str]
    model: FieldModel | None = None
    settings: ReadingSettings = field(default_factory=ReadingSettings)

    @property
    def reading_settings(self) -> ReadingSettings:
        return replace(self.settings, own_vat_ids=self.own_vat_ids | self.settings.own_vat_ids)


@dataclass(frozen=True)
class Source:
    """One reading and where it comes from in the email."""

    reading: InvoiceReading
    position: int | None  # the attachment, from 1; None for the email text
    pages: tuple[int, ...] = ()  # the pages of the file this invoice stands on


@dataclass(frozen=True)
class FoundInvoice:
    key: str
    kind: CaseKind
    source: Source  # the reading the case's data comes from
    copies: tuple[Source, ...] = ()  # the PDF copy of an e-invoice, for the person who checks


@dataclass(frozen=True)
class EmailInvoices:
    invoices: tuple[FoundInvoice, ...]
    doubts: tuple[str, ...]  # why the invoices cannot be taken as found
    attachment_sha256: tuple[str, ...]  # by position, from 1


def case_key(reading: InvoiceReading) -> str | None:
    """``vat:<VAT ID>|<number>``, or ``name:<issuer>|<number>`` when the invoice states no VAT
    ID. None when the number or the issuer cannot be read."""
    number = reading.value("invoice_number")
    number = re.sub(r"\s", "", str(number)).upper() if number else ""
    vat_id = reading.value("vendor_vat_id")
    if vat_id:
        issuer = f"vat:{clean_vat_id(str(vat_id))}"
    else:
        name = _plain(normalise(str(reading.value("issuer_name") or "")))
        issuer = f"name:{name}" if name else ""
    if not number or not issuer or "|" in number:
        return None
    return f"{issuer}|{number}"


def _plain(name: str) -> str:
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(umlaut, plain)
    return name


def _position(document: str) -> int:
    found = re.match(r"attachment (\d+)", document)
    if found is None:
        raise ValueError(f"not an attachment: {document}")
    return int(found[1])


def find_invoices(raw: bytes, invoicing: Invoicing) -> EmailInvoices:
    """Every invoice in the email, read page by page. Calls the reader and the model, so never
    call it inside a unit of work. Raises ``ReaderUnavailable`` or ``ModelUnavailable`` when a
    service cannot be reached; the email then stays where it is and the next sweep tries again."""
    settings = invoicing.reading_settings
    attachments = attachments_of(raw)
    datas = [a.data for a in attachments]
    doubts: list[str] = []
    sources: list[Source] = []
    for position, data in enumerate(datas, start=1):
        found = read_attachment_invoices(
            position, data, invoicing.reader, invoicing.model, settings
        )
        if found is None:
            continue
        doubts += found.doubts
        sources += [Source(reading, position, pages) for pages, reading in found.readings]
    message = message_from_bytes(raw, policy=policy.default)
    if isinstance(message, EmailMessage):
        body = read_body(body_of(message), invoicing.model, settings)
        if body is not None:
            sources.append(Source(body, None))

    for source in sources:
        if source.reading.unreadable is not None:
            doubts.append(f"the {source.reading.document} cannot be read")
    einvoices = read_einvoices(datas)
    doubts += [f"the e-invoice in the {r.document} cannot be read" for r in einvoices.refused]

    by_reading = {id(s.reading): s for s in sources}
    paired, alone = pair(einvoices.einvoices, [s.reading for s in sources])
    invoices: list[tuple[InvoiceReading, Source, tuple[Source, ...]]] = []
    for item in paired:
        xml = Source(item.einvoice.reading, _position(item.einvoice.document))
        copy = by_reading.get(id(item.pdf)) if item.pdf is not None else None
        for disagreement in item.disagreements:
            doubts.append(
                f"the PDF copy of the e-invoice in the {item.einvoice.document} states another "
                f"{disagreement.field.replace('_', ' ')}"
            )
        invoices.append((item.einvoice.reading, xml, (copy,) if copy else ()))
    for reading in alone:
        if reading.unreadable is not None:
            continue
        if reading.kind in INVOICE_KINDS:
            invoices.append((reading, by_reading[id(reading)], ()))
        elif reading.kind == "unknown" and (
            reading.value("invoice_number") or reading.value("gross_amount")
        ):
            # Looks like an invoice without an invoice title ("Rechnung Nr. 1001 vom ..."). It
            # never silently gets no case (EX-01, same rule as PR 104).
            doubts.append(
                f"{_where(by_reading[id(reading)])} states an invoice number or a total but no"
                " document title ARGUS knows"
            )

    found_invoices: list[FoundInvoice] = []
    keys: list[str] = []
    for reading, source, copies in invoices:
        where = _where(source)
        key = case_key(reading)
        if key is None:
            doubts.append(f"the invoice in {where} has no invoice number or issuer ARGUS can read")
            continue
        if key in keys:
            doubts.append(f"the invoice in {where} is in this email twice")
            continue
        keys.append(key)
        kind = (
            CaseKind.INCOMING_CREDIT_NOTE
            if reading.kind in CREDIT_KINDS
            else CaseKind.INCOMING_INVOICE
        )
        found_invoices.append(FoundInvoice(key, kind, source, copies))
    return EmailInvoices(
        tuple(found_invoices),
        tuple(doubts),
        tuple(hashlib.sha256(data).hexdigest() for data in datas),
    )


def _where(source: Source) -> str:
    """Names the document by position and page, never by file name (NF-03)."""
    if len(source.pages) == 1:
        return f"the {source.reading.document}, page {source.pages[0]}"
    if source.pages:
        return f"the {source.reading.document}, pages {source.pages[0]} to {source.pages[-1]}"
    return f"the {source.reading.document}"


def doubts_about(found: EmailInvoices, model_invoices: int | None) -> tuple[str, ...]:
    """Why the email must go to a person instead of becoming cases: the doubts of reading it,
    and a model that lists another number of invoices than the documents and pages show."""
    doubts = list(found.doubts)
    if model_invoices is None:
        doubts.append("the language model did not say how many invoices the email holds")
    elif model_invoices != len(found.invoices) or not found.invoices:
        doubts.append(
            f"the language model lists {model_invoices} invoices, the documents and pages "
            f"show {len(found.invoices)}"
        )
    return tuple(doubts)


def to_manual_review(decision: SortingDecision, doubts: Sequence[str]) -> SortingDecision:
    """The same decision, sent to a person with the reasons (IN-09)."""
    return replace(
        decision,
        route=Route.MANUAL_REVIEW,
        category=None,
        reasons=(*decision.reasons, *doubts),
    )


def open_cases(connection: Connection, email_id: int, found: EmailInvoices) -> list[int]:
    """Open one case per invoice for the email, link each to its own document and store its
    reading. Run inside the unit of work that records the decision. Returns the case IDs."""
    case_ids = []
    for invoice in found.invoices:
        reading = invoice.source.reading
        case_id = create_case(
            connection,
            invoice.kind,
            source_email_id=email_id,
            document_number=str(reading.value("invoice_number")),
            gross_cents=_gross_cents(reading),
            currency=_currency(reading),
            case_key=invoice.key,
        )
        linked: set[int] = set()
        for role, source in (("invoice", invoice.source), *(("copy", c) for c in invoice.copies)):
            document_id = _document_id(connection, email_id, found, source.position)
            if document_id is not None and document_id not in linked:
                link_document_to_case(connection, case_id, document_id)
                linked.add(document_id)
            _store_reading(connection, case_id, document_id, role, source)
        case_ids.append(case_id)
    return case_ids


def _gross_cents(reading: InvoiceReading) -> int | None:
    """The gross amount only when no person still has to check it; otherwise the case waits for
    the person, who sees the flag in the reading."""
    found = reading.fields.get("gross_amount")
    if found is None or found.value is None or found.flag is not None:
        return None
    return to_cents(str(found.value))


def _currency(reading: InvoiceReading) -> str | None:
    found = reading.fields.get("currency")
    if found is None or found.flag is not None or not re.fullmatch(r"[A-Z]{3}", str(found.value)):
        return None
    return str(found.value)


def _document_id(
    connection: Connection, email_id: int, found: EmailInvoices, position: int | None
) -> int | None:
    if position is None:
        return None
    document_id = connection.execute(
        text(
            "select id from stored_document"
            " where email_id = :email_id and kind = 'attachment' and sha256 = :sha256"
        ),
        {"email_id": email_id, "sha256": found.attachment_sha256[position - 1]},
    ).scalar_one_or_none()
    if document_id is None:
        raise RuntimeError(f"attachment {position} of email record {email_id} is not stored")
    return int(document_id)


def _store_reading(
    connection: Connection, case_id: int, document_id: int | None, role: str, source: Source
) -> None:
    connection.execute(
        text(
            """
            insert into invoice_reading (case_id, document_id, document, pages, role, reading)
            values (:case_id, :document_id, :document, :pages, :role, cast(:reading as jsonb))
            """
        ),
        {
            "case_id": case_id,
            "document_id": document_id,
            "document": source.reading.document,
            "pages": list(source.pages),
            "role": role,
            "reading": _json(source.reading),
        },
    )


def _json(reading: InvoiceReading) -> str:
    return json.dumps(reading.as_record(), default=str)
