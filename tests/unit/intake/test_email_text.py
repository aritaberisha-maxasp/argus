"""The sweep reads each stored email for the sorting with the readers of #28 and #29 (#91).

Covers IN-04: sender, subject, body, headers and the text of each attachment.
"""

import pytest

from argus.intake.email_text import EmailReader
from argus.reading.pages import Analysis, DocumentNotReadable, Page, ReaderUnavailable
from tests.unit.sorting_helpers import EMAILS


def fixture(email_id: str) -> bytes:
    return next(EMAILS.glob(f"{email_id}_*.eml")).read_bytes()


class Reader:
    """A document reader that answers with fixed text, or fails as told."""

    name = "test"

    def __init__(self, text: str = "Rechnung", error: Exception | None = None) -> None:
        self.text, self.error = text, error
        self.read_types: list[str] = []

    def read(self, data: bytes, content_type: str) -> Analysis:
        self.read_types.append(content_type)
        if self.error is not None:
            raise self.error
        return Analysis(pages=(Page(1, self.text),))


@pytest.mark.req("IN-04")
def test_the_sender_subject_body_and_pdf_text_are_read() -> None:
    reader = Reader("Muster Bürobedarf GmbH\nRechnung")
    email = EmailReader(reader)(fixture("E01"))

    assert email.sender_address and email.sender_address == email.sender_address.lower()
    assert email.subject and email.body
    assert email.headers.get("message-id") == email.message_id
    assert reader.read_types == ["application/pdf"]
    assert [a.text for a in email.attachments] == ["Muster Bürobedarf GmbH\nRechnung"]


@pytest.mark.req("IN-02", "IN-04")
def test_an_xrechnung_is_read_by_the_einvoice_reader_not_the_pdf_reader() -> None:
    reader = Reader()
    email = EmailReader(reader)(fixture("E08"))

    [xml] = [a for a in email.attachments if a.content_type.endswith("xml")]
    assert reader.read_types == []
    assert xml.text is not None
    lines = xml.text.splitlines()
    assert lines[2] == "Rechnung" and lines[3].startswith("Nummer: ")
    assert "Max ASP" in lines[1]


@pytest.mark.req("IN-04", "IN-09")
def test_an_unreadable_document_has_no_text() -> None:
    email = EmailReader(Reader(error=DocumentNotReadable("broken")))(fixture("E01"))
    assert [a.text for a in email.attachments] == [None]


@pytest.mark.req("IN-04")
def test_a_reader_that_cannot_be_reached_stops_the_sorting_of_this_email() -> None:
    with pytest.raises(ReaderUnavailable):
        EmailReader(Reader(error=ReaderUnavailable("down")))(fixture("E01"))
