"""Find the XML inside a ZUGFeRD or Factur-X PDF (#28, NF-02).

A hybrid invoice is a PDF/A-3 with the e-invoice XML attached inside it. The PDF is the copy a
person reads; the XML is what ARGUS books from (ADR 0006 point 1).
"""

import io
import logging

from pypdf import PdfReader

log = logging.getLogger("argus.einvoice")

# The names the standards give the embedded XML (Factur-X, ZUGFeRD 2, XRechnung), in lower case.
EMBEDDED_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml")


def embedded_xml(pdf: bytes) -> bytes | None:
    """The e-invoice XML embedded in ``pdf``, or None when there is none. A PDF that pypdf
    cannot open has none as far as this step is concerned: the PDF reader (#29) reports it."""
    try:
        attachments = PdfReader(io.BytesIO(pdf)).attachments
        found = {name.lower(): name for name in attachments}
        for wanted in EMBEDDED_NAMES:
            if wanted in found:
                contents = attachments[found[wanted]]
                return contents[0] if contents else None
    # A broken or hostile PDF can make pypdf raise almost anything. It must never stop the run.
    except Exception as error:
        log.info("The embedded files of a PDF could not be listed: %s", type(error).__name__)
    return None
