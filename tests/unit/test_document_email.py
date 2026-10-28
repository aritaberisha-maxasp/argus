"""Splitting a received email into the files ARGUS keeps (issue #20). Runs without Azurite,
on the in memory store that has the same rules."""

import base64
import hashlib
from pathlib import Path

import pytest

from argus.documents.email import EML_CONTENT_TYPE, attachments_of, store_email
from argus.documents.store import (
    DocumentNotFound,
    FingerprintMismatch,
    MemoryDocumentStore,
    location_of,
)

EMAILS = Path(__file__).resolve().parents[1] / "fixtures" / "emails"


def fixture(name: str) -> bytes:
    return (EMAILS / f"{name}.eml").read_bytes()


@pytest.mark.req("NF-01")
def test_the_raw_email_is_stored_as_it_is() -> None:
    store = MemoryDocumentStore()
    raw = fixture("E01_buerobedarf-rechnung")
    stored = store_email(store, raw)
    assert stored.raw.sha256 == hashlib.sha256(raw).hexdigest()
    assert stored.raw.content_type == EML_CONTENT_TYPE
    assert store.get(stored.raw.sha256) == raw


@pytest.mark.req("IN-08", "NF-01")
def test_every_attachment_is_stored_in_order() -> None:
    stored = store_email(MemoryDocumentStore(), fixture("E14_drei-rechnungen-eine-mail"))
    assert [a.filename for a in stored.attachments] == [
        "Rechnung_R-2026-0932.pdf",
        "Rechnung_R-2026-0933.pdf",
        "Rechnung_R-2026-0934.pdf",
    ]
    assert len({a.document.sha256 for a in stored.attachments}) == 3


@pytest.mark.req("NF-01")
def test_an_email_without_attachments_keeps_only_the_raw_file() -> None:
    store = MemoryDocumentStore()
    stored = store_email(store, fixture("E16_rechnung-im-mailtext"))
    assert stored.attachments == []
    assert list(store.files) == [stored.raw.location]


@pytest.mark.req("NF-02")
def test_an_xml_attachment_is_not_converted() -> None:
    raw = fixture("E08_cloud-hosting-xrechnung")
    [xml] = attachments_of(raw)
    assert xml.content_type == "application/xml"
    assert xml.data.lstrip().startswith(b"<?xml")
    # The base64 in the email decodes to exactly these bytes; nothing is parsed or rewritten.
    assert xml.data == _base64_body_after(raw, b"Rechnung_BCH-26-10-0057_xrechnung.xml")


@pytest.mark.req("NF-01")
def test_an_attachment_of_a_forwarded_email_is_found() -> None:
    inner = (
        b"From: lieferant@example.com\r\nSubject: Rechnung\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="in"\r\n\r\n--in\r\n'
        b"Content-Type: text/plain\r\n\r\nAnbei.\r\n--in\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="R-1.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--in--\r\n"
    )
    outer = (
        b"From: kollege@example.com\r\nSubject: Fwd\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="out"\r\n\r\n--out\r\n'
        b"Content-Type: text/plain\r\n\r\nSiehe Anhang.\r\n--out\r\n"
        b"Content-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--out--\r\n"
    )
    [pdf] = attachments_of(outer)
    assert (pdf.filename, pdf.data) == ("R-1.pdf", b"%PDF-1.4\n")


@pytest.mark.req("NF-01")
def test_storing_again_returns_the_first_copy() -> None:
    store = MemoryDocumentStore()
    first = store.put(b"same bytes", "application/pdf")
    again = store.put(b"same bytes", "text/plain")
    assert again == first
    assert len(store.files) == 1


@pytest.mark.req("NF-01")
def test_a_changed_file_is_noticed() -> None:
    store = MemoryDocumentStore()
    stored = store.put(b"original", "application/pdf")
    store.files[stored.location] = (b"changed", "application/pdf")
    with pytest.raises(FingerprintMismatch):
        store.get(stored.sha256)
    with pytest.raises(FingerprintMismatch):
        store.put(b"original", "application/pdf")


def test_a_missing_file_is_reported() -> None:
    with pytest.raises(DocumentNotFound):
        MemoryDocumentStore().get("f" * 64)


@pytest.mark.parametrize("bad", ["", "ABC", "a" * 63, "g" * 64, "../" + "a" * 61])
def test_only_a_real_fingerprint_names_a_file(bad: str) -> None:
    with pytest.raises(ValueError, match="64 lower case hex"):
        location_of(bad)


def _base64_body_after(raw: bytes, filename: bytes) -> bytes:
    """Decode the attachment body by hand, independent of the email parser used by ARGUS."""
    header_end = raw.index(b"\n\n", raw.index(filename)) + 2
    body = raw[header_end:].split(b"\n--", 1)[0]
    return base64.b64decode(b"".join(body.split()))
