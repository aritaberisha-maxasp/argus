"""Turn a stored email into what the sorting reads (IN-04): sender, subject, body, headers and
the text of each attachment.

The raw email comes from the document store, byte for byte as it arrived. PDFs and images are
read by the document reader of #29 (Document Intelligence in production, the local reader in
development). E-invoice XML is read by the e-invoice reader of #28 and laid out like the first
lines of a PDF invoice: issuer, recipient, title and number, which is what the checks of the
sorting look at. Any other file has no text.

A document the reader cannot read gets no text, and the sorting decides with what is left (an
invoice that cannot be read goes to manual review). A reader that cannot be reached raises
``ReaderUnavailable``, so the email stays where it is and the next sweep tries again.
"""

import email
import re
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

from argus.documents.email import attachments_of
from argus.einvoice.parser import EInvoiceRefused, looks_like_xml, read_xml
from argus.reading.invoice import sniff_type
from argus.reading.pages import DocumentNotReadable, DocumentReader
from argus.sorting.models import Attachment, EmailToSort
from argus.sorting.sender import first_headers

TITLE_FOR_KIND = {"invoice": "Rechnung", "credit_note": "Gutschrift"}


class EmailReader:
    """Reads a raw email for the sorting with one document reader."""

    def __init__(self, reader: DocumentReader) -> None:
        self._reader = reader

    def __call__(self, raw: bytes) -> EmailToSort:
        message = email.message_from_bytes(raw, policy=policy.default)
        if not isinstance(message, EmailMessage):
            raise TypeError("the stored email cannot be parsed")
        name, address = parseaddr(str(message.get("From", "")))
        return EmailToSort(
            message_id=str(message.get("Message-ID", "")).strip(),
            sender_address=address.lower(),
            sender_name=name,
            subject=str(message.get("Subject", "")),
            body=body_of(message),
            headers=first_headers(message.items()),
            attachments=tuple(
                Attachment(
                    a.filename or "attachment", a.content_type, self.text_of(position, a.data)
                )
                for position, a in enumerate(attachments_of(raw), start=1)
            ),
        )

    def text_of(self, position: int, data: bytes) -> str | None:
        content_type = sniff_type(data)
        if content_type is not None:
            try:
                return self._reader.read(data, content_type).text
            except DocumentNotReadable:
                return None
        if looks_like_xml(data):
            return einvoice_text(position, data)
        return None


def body_of(message: EmailMessage) -> str:
    """The plain text body, or the HTML body without its tags."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    body = part.get_content()
    if not isinstance(body, str):
        return ""
    if part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    return body


def einvoice_text(position: int, data: bytes) -> str | None:
    """The first lines of an e-invoice as a PDF would show them, or None when the XML is not an
    e-invoice ARGUS can read."""
    try:
        einvoice = read_xml(f"attachment {position}", data)
    except EInvoiceRefused:
        return None
    if einvoice is None:
        return None
    reading = einvoice.reading
    lines = [
        reading.value("issuer_name"),
        reading.value("recipient_name"),
        TITLE_FOR_KIND.get(reading.kind, "Rechnung"),
        f"Nummer: {reading.value('invoice_number')}",
    ]
    return "\n".join(str(line) for line in lines if line)
