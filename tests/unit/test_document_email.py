"""Splitting a received email into the files ARGUS keeps (issue #20). Runs without Azurite,
on the in memory store that has the same rules."""

import base64
import hashlib
import logging
from pathlib import Path

import pytest

from argus.documents.email import (
    EML_CONTENT_TYPE,
    EmailNotReadable,
    attachments_of,
    store_email,
)
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


# --- #90: an email that cannot be split never shows its headers in an error ---

VENDOR_HEADER = "Secret Vendor GmbH <billing@secret-vendor.example>"


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("where", ["message_from_bytes", "_is_attachment"])
def test_an_error_while_splitting_never_quotes_header_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, where: str
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise ValueError(f"cannot parse header From: {VENDOR_HEADER}")

    monkeypatch.setattr(f"argus.documents.email.{where}", broken)
    raw = b"From: " + VENDOR_HEADER.encode() + b"\r\nSubject: Rechnung 4711\r\n\r\nHallo\r\n"
    log = logging.getLogger("argus.test")
    with pytest.raises(EmailNotReadable, match="ValueError at part") as raised:
        try:
            attachments_of(raw)
        except EmailNotReadable:
            log.exception("Mailbox sweep failed on email %s, next sweep retries", "AAk-1")
            raise
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    logged = caplog.text
    assert "Mailbox sweep failed" in logged
    for leak in ("Secret Vendor", "secret-vendor", "Rechnung 4711"):
        assert leak not in str(raised.value)
        assert leak not in logged


@pytest.mark.req("NF-03")
def test_a_readable_email_is_split_as_before() -> None:
    raw = (
        b"Content-Type: multipart/mixed; boundary=X\r\n\r\n--X\r\nContent-Type: text/plain\r\n\r\n"
        b"Hallo\r\n--X\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="r.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--X--\r\n"
    )
    assert [(a.data, a.filename) for a in attachments_of(raw)] == [(b"%PDF", "r.pdf")]
