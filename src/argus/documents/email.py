"""Split a received email into the files ARGUS keeps: the raw ``.eml`` and each attachment.

The raw email is stored exactly as it arrived. Each attachment is stored as the file the sender
attached: its transfer encoding (base64, quoted printable) is undone and nothing else. An
e-invoice XML is kept as the XML that arrived (NF-02), and a ZUGFeRD PDF keeps its embedded XML
because the PDF itself is stored unchanged. Nothing is ever converted.

When an email cannot be split, the error never quotes its headers or content: the sweep logs
errors with ``log.exception``, and a header can hold a sender, a subject or a file name
(NF-03, #90).
"""

from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage, Message

from argus.documents.store import DocumentStore, StoredDocument

EML_CONTENT_TYPE = "message/rfc822"


class EmailNotReadable(ValueError):
    """The email could not be split into its files. The message names only the kind of error
    and the position of the part, never header text or content (NF-03)."""


@dataclass(frozen=True)
class StoredAttachment:
    document: StoredDocument
    filename: str | None


@dataclass(frozen=True)
class StoredEmail:
    raw: StoredDocument
    attachments: list[StoredAttachment]


@dataclass(frozen=True)
class Attachment:
    data: bytes
    content_type: str
    filename: str | None


def attachments_of(raw: bytes) -> list[Attachment]:
    """Every attached file, in the order it appears, including attachments of an email that was
    forwarded as an attachment. The text of the email itself is not an attachment."""
    position = 0
    try:
        message = message_from_bytes(raw, policy=policy.default)
        found = []
        for index, part in enumerate(message.walk()):
            position = index
            if part.is_multipart() or not _is_attachment(part):
                continue
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                continue
            found.append(Attachment(data, part.get_content_type(), part.get_filename()))
    except Exception as error:
        # "from None" drops the original error, whose text may quote a header (NF-03).
        raise EmailNotReadable(
            f"the email cannot be split into its files: {type(error).__name__} at part {position}"
        ) from None
    return found


def _is_attachment(part: Message) -> bool:
    if not isinstance(part, EmailMessage):
        return False
    return part.get_content_disposition() == "attachment" or part.get_filename() is not None


def store_email(store: DocumentStore, raw: bytes) -> StoredEmail:
    """Store the raw email and each attachment. Safe to repeat after a crash: files already in
    the store are found, never replaced. Call it outside any ``unit_of_work``."""
    stored_raw = store.put(raw, EML_CONTENT_TYPE)
    attachments = [
        StoredAttachment(store.put(a.data, a.content_type), a.filename) for a in attachments_of(raw)
    ]
    return StoredEmail(stored_raw, attachments)
