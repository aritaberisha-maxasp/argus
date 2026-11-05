"""Split one file into its invoices, page by page (#30, IN-08).

Suppliers sometimes put several invoices into one PDF. The spike once read two invoices on two
pages of one PDF as one (H04), so a file is never read as a whole when its pages say otherwise:

- A page that states an invoice number the pages before it did not state starts a new invoice,
  and so does a page with the same number but a VAT ID the pages before it did not state: two
  vendors that both use number 1001 are two invoices.
- A page without an invoice number belongs to the invoice before it (a second page, terms and
  conditions). Pages before the first number belong to the first invoice.
- A page that states two different invoice numbers cannot be split by page. ARGUS does not guess
  where one ends, so the email goes to a person.

Document Intelligence also says how many invoices it sees and on which pages. When that does not
fit the split of the pages, the email goes to a person too. Each part gets the reader's fields of
the one invoice the reader found on exactly those pages, and our own rules fill the rest.
"""

from dataclasses import dataclass

from argus.reading.fields import ReadField
from argus.reading.pages import Analysis, InvoiceSpan, Page
from argus.reading.text_rules import invoice_numbers, vat_ids


@dataclass(frozen=True)
class FilePart:
    """The pages of one invoice in a file, as an analysis of their own."""

    pages: tuple[int, ...]  # page numbers in the file, from 1
    analysis: Analysis


@dataclass(frozen=True)
class Split:
    parts: tuple[FilePart, ...]
    # Why the file cannot be split safely. Any doubt sends the email to manual review.
    doubts: tuple[str, ...] = ()


def split_file(
    analysis: Analysis, document: str, own_vat_ids: frozenset[str] = frozenset()
) -> Split:
    """The invoices of one file, page by page. ``document`` names the file by position
    ("attachment 2") for the doubts, never by file name (NF-03). ``own_vat_ids`` are Max ASP's,
    which a page may print as the customer's and which never start a new invoice."""
    doubts: list[str] = []
    groups: list[list[Page]] = []
    numbers_of_group: list[list[str]] = []
    issuers_of_group: list[list[str]] = []
    for page in analysis.pages:
        numbers = invoice_numbers((page,))
        issuers = vat_ids(page, own_vat_ids)
        if len(numbers) > 1:
            doubts.append(
                f"page {page.number} of {document} states {len(numbers)} invoice numbers"
                " and cannot be split by page"
            )
        starts_new = (
            bool(groups)
            and bool(numbers_of_group[-1])
            and bool(numbers)
            and (
                any(n not in numbers_of_group[-1] for n in numbers)
                # The same number from another issuer is another invoice (two vendors, both 1001).
                or any(i not in issuers_of_group[-1] for i in issuers)
            )
        )
        if not groups or starts_new:
            groups.append([page])
            numbers_of_group.append(list(numbers))
            issuers_of_group.append(list(issuers))
        else:
            groups[-1].append(page)
            numbers_of_group[-1] += [n for n in numbers if n not in numbers_of_group[-1]]
            issuers_of_group[-1] += [i for i in issuers if i not in issuers_of_group[-1]]

    if len(groups) <= 1:
        if analysis.invoice_count is not None and analysis.invoice_count > 1:
            doubts.append(
                f"the reader sees {analysis.invoice_count} invoices in {document}, "
                "its pages show one"
            )
        pages = tuple(p.number for p in analysis.pages)
        return Split((FilePart(pages, analysis),), tuple(doubts))

    parts = [tuple(p.number for p in group) for group in groups]
    doubts += _reader_doubts(analysis, parts, document)
    return Split(
        tuple(
            FilePart(
                pages,
                Analysis(
                    pages=tuple(group),
                    fields=_fields_for(analysis.invoices, pages),
                    text_confidence=analysis.text_confidence,
                    invoice_count=None,
                ),
            )
            for pages, group in zip(parts, groups, strict=True)
        ),
        tuple(doubts),
    )


def _reader_doubts(analysis: Analysis, parts: list[tuple[int, ...]], document: str) -> list[str]:
    """Where the reader's own count or pages do not fit the split of the pages."""
    if analysis.invoice_count is None:
        return []
    if analysis.invoice_count != len(parts):
        return [
            f"the reader sees {analysis.invoice_count} invoices in {document}, "
            f"its pages show {len(parts)}"
        ]
    for span in analysis.invoices:
        if span.pages and not any(span.pages <= set(pages) for pages in parts):
            return [f"the reader puts one invoice of {document} across two invoices of its pages"]
    return []


def _fields_for(spans: tuple[InvoiceSpan, ...], pages: tuple[int, ...]) -> dict[str, ReadField]:
    """The reader's fields for these pages: those of the one invoice it found on exactly these
    pages. None when it did not say where its invoices stand; our own rules read the fields."""
    inside = [span for span in spans if span.pages and span.pages <= set(pages)]
    return dict(inside[0].fields) if len(inside) == 1 else {}
