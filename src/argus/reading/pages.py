"""What a document reader gives back: the text of each page, and the fields it found itself."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from argus.reading.fields import ReadField


class ReaderUnavailable(RuntimeError):
    """The reader could not be reached or failed for a reason that is not the document's. The
    email stays where it is and the next sweep tries again. The message never holds content."""


class DocumentNotReadable(ValueError):
    """The reader looked at the document and says it cannot be read: a broken file, a format it
    does not take, an empty page. The document goes to the exception queue (EX-02)."""


@dataclass(frozen=True)
class Page:
    number: int  # from 1
    text: str


@dataclass(frozen=True)
class Analysis:
    pages: tuple[Page, ...]
    fields: Mapping[str, ReadField] = field(default_factory=dict)
    # How sure the reader is about the words, from 0 to 1 (OCR word confidence). None for a
    # PDF text layer, which is exact.
    text_confidence: float | None = None
    # How many invoices the reader sees in the file. None when it does not say.
    invoice_count: int | None = None

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)


class DocumentReader(Protocol):
    name: str

    def read(self, data: bytes, content_type: str) -> Analysis:
        """Read one PDF or image. Raises :class:`ReaderUnavailable` or
        :class:`DocumentNotReadable`."""
        ...
