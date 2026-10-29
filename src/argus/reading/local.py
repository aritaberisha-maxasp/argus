"""The local reader for development and CI: the PDF text layer, and Tesseract OCR for images
and for PDFs without text (ADR 0006 point 2). Production reads with Document Intelligence.

Tesseract is a program, not a Python package: ``apt-get install tesseract-ocr
tesseract-ocr-deu`` on Debian and Ubuntu. It gets the document on stdin, so nothing is written
to disk.
"""

import io
import logging
import shutil
import subprocess

import pypdf
from pypdf.errors import PyPdfError

from argus.reading.pages import Analysis, DocumentNotReadable, Page, ReaderUnavailable

log = logging.getLogger("argus.reading")

# A PDF page with fewer readable characters than this is treated as a scan and goes to OCR.
MIN_TEXT_LAYER_CHARS = 40
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/tiff", "image/gif", "image/bmp"})
OCR_TIMEOUT_SECONDS = 120


class LocalReader:
    name = "local"

    def __init__(self, tesseract: str = "tesseract", languages: str = "deu+eng") -> None:
        self._tesseract = tesseract
        self._languages = languages

    def read(self, data: bytes, content_type: str) -> Analysis:
        if content_type == "application/pdf":
            return self._read_pdf(data)
        if content_type in IMAGE_TYPES:
            text, confidence = self.ocr(data)
            return Analysis(pages=(Page(1, text),), text_confidence=confidence)
        raise DocumentNotReadable(f"ARGUS does not read {content_type} documents")

    def _read_pdf(self, data: bytes) -> Analysis:
        try:
            pdf = pypdf.PdfReader(io.BytesIO(data))
            texts = [page.extract_text() or "" for page in pdf.pages]
        except (PyPdfError, ValueError, KeyError, TypeError) as error:
            raise DocumentNotReadable(f"the PDF cannot be opened: {type(error).__name__}") from None
        pages: list[Page] = []
        confidences: list[float] = []
        for number, (page, text) in enumerate(zip(pdf.pages, texts, strict=True), start=1):
            if len("".join(text.split())) >= MIN_TEXT_LAYER_CHARS:
                pages.append(Page(number, text))
                continue
            # A scanned page: OCR the images on it.
            ocr_texts = []
            for image in page.images:
                ocr_text, confidence = self.ocr(image.data)
                ocr_texts.append(ocr_text)
                confidences.append(confidence)
            pages.append(Page(number, "\n".join([text, *ocr_texts]).strip()))
        if not pages:
            raise DocumentNotReadable("the PDF has no pages")
        return Analysis(
            pages=tuple(pages), text_confidence=min(confidences) if confidences else None
        )

    def ocr(self, image: bytes) -> tuple[str, float]:
        """Text and mean word confidence from 0 to 1."""
        if shutil.which(self._tesseract) is None:
            raise ReaderUnavailable("Tesseract is not installed")
        try:
            result = subprocess.run(  # noqa: S603  fixed arguments, the image goes on stdin
                [self._tesseract, "stdin", "stdout", "-l", self._languages, "tsv"],
                input=image,
                capture_output=True,
                check=True,
                timeout=OCR_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise ReaderUnavailable("Tesseract took too long") from None
        except subprocess.CalledProcessError:
            raise DocumentNotReadable("Tesseract cannot read the image") from None
        lines: dict[tuple[str, str, str], list[str]] = {}
        confidences: list[float] = []
        for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
                continue
            lines.setdefault((cols[2], cols[3], cols[4]), []).append(cols[11])
            confidences.append(float(cols[10]))
        text = "\n".join(" ".join(words) for words in lines.values())
        confidence = sum(confidences) / len(confidences) / 100 if confidences else 0.0
        return text, max(0.0, min(1.0, confidence))
