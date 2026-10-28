"""Record stored files in the database and link them to cases (migration 0003).

Call these inside a short :func:`argus.cases.store.unit_of_work`, after the files are in blob
storage. The database writes the ``document_stored`` and ``document_linked`` audit events with
the fingerprint in the same transaction; the application cannot write them itself.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection

from argus.cases.store import _run
from argus.documents.email import StoredEmail
from argus.documents.store import StoredDocument

DocumentKind = Literal["email", "attachment"]


@dataclass(frozen=True)
class CaseDocument:
    document_id: int
    kind: DocumentKind
    sha256: str
    location: str
    size: int
    content_type: str
    filename: str | None
    linked_at: datetime


def record_document(
    connection: Connection,
    email_id: int,
    kind: DocumentKind,
    document: StoredDocument,
    filename: str | None = None,
) -> int:
    """Record one stored file of an email. Safe to repeat: the same file of the same email gives
    back the row that is already there. Returns the document ID."""
    params = {
        "email_id": email_id,
        "kind": kind,
        "sha256": document.sha256,
        "location": document.location,
        "size_bytes": document.size,
        "content_type": document.content_type,
        "filename": filename,
    }
    inserted = _run(
        connection,
        """
        insert into stored_document (email_id, kind, sha256, location, size_bytes, content_type,
                                     filename)
        values (:email_id, :kind, :sha256, :location, :size_bytes, :content_type, :filename)
        on conflict (email_id, kind, sha256) do nothing
        returning id
        """,
        params,
    ).scalar()
    if inserted is not None:
        return int(inserted)
    existing: int = _run(
        connection,
        "select id from stored_document where email_id = :email_id and kind = :kind"
        " and sha256 = :sha256",
        params,
    ).scalar_one()
    return existing


def record_email_documents(connection: Connection, email_id: int, stored: StoredEmail) -> list[int]:
    """Record the raw email and its attachments. Returns the document IDs, raw email first.
    An email has one raw file: recording different bytes for the same email is refused.

    The same file attached twice to one email, even under two names, is one record on purpose:
    the bytes are what GoBD keeps. It keeps the first file name and its ID appears twice."""
    ids = [record_document(connection, email_id, "email", stored.raw)]
    ids += [
        record_document(connection, email_id, "attachment", a.document, a.filename)
        for a in stored.attachments
    ]
    return ids


def link_document_to_case(connection: Connection, case_id: int, document_id: int) -> None:
    """Record that a case is based on this file, so its fingerprint is on the case. Safe to
    repeat. The file must have come with the case's email."""
    _run(
        connection,
        """
        insert into case_document (case_id, document_id) values (:case_id, :document_id)
        on conflict do nothing
        """,
        {"case_id": case_id, "document_id": document_id},
    )


def case_documents(connection: Connection, case_id: int) -> list[CaseDocument]:
    """The files a case is based on, with their fingerprints, in the order they were linked."""
    rows = _run(
        connection,
        """
        select d.id, d.kind, d.sha256, d.location, d.size_bytes, d.content_type, d.filename,
               l.linked_at
        from case_document l join stored_document d on d.id = l.document_id
        where l.case_id = :case_id
        order by l.linked_at, d.id
        """,
        {"case_id": case_id},
    ).mappings()
    return [
        CaseDocument(
            document_id=r["id"],
            kind=r["kind"],
            sha256=r["sha256"],
            location=r["location"],
            size=r["size_bytes"],
            content_type=r["content_type"],
            filename=r["filename"],
            linked_at=r["linked_at"],
        )
        for r in rows
    ]
