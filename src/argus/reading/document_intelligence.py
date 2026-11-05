"""Azure AI Document Intelligence reads the text and the invoice fields (ADR 0006 point 2,
IN-02, IN-03, PR-01, NF-03).

The prebuilt invoice model gives every field with a confidence and the page it stands on, and
the text of every page, in one call. It runs in Max ASP's own Azure tenant in an EU region; the
infrastructure code (#65) pins the region. In CI a recorded stand in answers instead, with
answers in the same format, so no test calls the real service.

Document content is never written to the application logs.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from argus.documents.store import fingerprint
from argus.reading.fields import Flag, ReadField, Source
from argus.reading.pages import (
    Analysis,
    DocumentNotReadable,
    InvoiceSpan,
    Page,
    ReaderUnavailable,
)
from argus.reading.text_rules import clean_vat_id, compact_iban, parse_rate, squash
from argus.secrets import SecretStore

log = logging.getLogger("argus.reading")

API_KEY_SECRET = "AZURE_DOCUMENT_INTELLIGENCE_API_KEY"  # noqa: S105  the name, not a secret
API_VERSION = "2024-11-30"
MODEL = "prebuilt-invoice"
# Error codes that mean the document itself cannot be read, not that the service failed.
UNREADABLE_CODES = frozenset(
    {"InvalidContent", "UnsupportedContent", "InvalidContentDimensions", "InvalidContentLength"}
)

# Document Intelligence field name to PR-01 field name, for the fields with one plain value.
SIMPLE_FIELDS = {
    "VendorName": "issuer_name",
    "CustomerName": "recipient_name",
    "VendorTaxId": "vendor_vat_id",
    "InvoiceId": "invoice_number",
    "InvoiceDate": "invoice_date",
    "DueDate": "due_date",
    "SubTotal": "net_amount",
    "TotalTax": "vat_amount",
    "InvoiceTotal": "gross_amount",
}


def _value(field: Mapping[str, Any]) -> Any:
    kind = field.get("type")
    if kind == "currency":
        amount = (field.get("valueCurrency") or {}).get("amount")
        if amount is None:
            return None
        try:
            # The service sends a JSON number; its shortest text form is the amount as printed.
            return str(Decimal(str(amount)).quantize(Decimal("0.01")))
        except InvalidOperation:
            return None
    if kind == "date":
        return field.get("valueDate")
    if kind == "number":
        number = field.get("valueNumber")
        return None if number is None else str(number)
    value = field.get("valueString")
    return squash(value) if isinstance(value, str) and value.strip() else None


def _page(field: Mapping[str, Any]) -> int | None:
    regions = field.get("boundingRegions") or []
    page = regions[0].get("pageNumber") if regions else None
    return page if isinstance(page, int) else None


def _read_field(name: str, field: Mapping[str, Any], value: Any) -> ReadField:
    confidence = field.get("confidence")
    return ReadField(
        name,
        value,
        Source.DOCUMENT_INTELLIGENCE,
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
        page=_page(field),
        words=squash(field["content"]) if isinstance(field.get("content"), str) else None,
    )


def _several(name: str, values: list[str]) -> ReadField:
    return ReadField(
        name,
        flag=Flag.SEVERAL_VALUES,
        note=f"the document states {len(values)} different values: {', '.join(values)}",
    )


def _fields(document: Mapping[str, Any]) -> dict[str, ReadField]:
    raw: Mapping[str, Any] = document.get("fields") or {}
    fields: dict[str, ReadField] = {}
    for di_name, name in SIMPLE_FIELDS.items():
        field = raw.get(di_name)
        if not field:
            continue
        value = _value(field)
        if name == "vendor_vat_id" and value:
            value = clean_vat_id(value)
        if value is not None:
            fields[name] = _read_field(name, field, value)
    # The currency comes with the total.
    total = raw.get("InvoiceTotal") or {}
    code = (total.get("valueCurrency") or {}).get("currencyCode")
    if isinstance(code, str) and code.strip():
        fields["currency"] = _read_field("currency", total, code.strip().upper())

    start, end = raw.get("ServiceStartDate"), raw.get("ServiceEndDate")
    if start and end and _value(start) and _value(end):
        period = _read_field("service_period", start, [_value(start), _value(end)])
        confidences = [c for c in (start.get("confidence"), end.get("confidence")) if c is not None]
        words = " - ".join(squash(f.get("content") or "") for f in (start, end))
        fields["service_period"] = ReadField(
            "service_period",
            period.value,
            Source.DOCUMENT_INTELLIGENCE,
            confidence=min(confidences) if confidences else None,
            page=period.page,
            words=words,
        )

    # IBANs sit in the list of payment details. Two different ones need a person.
    ibans: list[tuple[str, Mapping[str, Any]]] = []
    for item in (raw.get("PaymentDetails") or {}).get("valueArray") or []:
        iban_field = (item.get("valueObject") or {}).get("IBAN")
        if iban_field and _value(iban_field):
            iban = compact_iban(_value(iban_field))
            if iban not in [i for i, _ in ibans]:
                ibans.append((iban, iban_field))
    if len(ibans) == 1:
        fields["iban_on_document"] = _read_field("iban_on_document", ibans[0][1], ibans[0][0])
    elif ibans:
        fields["iban_on_document"] = _several("iban_on_document", [i for i, _ in ibans])

    # VAT rates sit in the tax details, one entry per rate (H03 of the spike had two).
    rates: list[tuple[str, Mapping[str, Any]]] = []
    for item in (raw.get("TaxDetails") or {}).get("valueArray") or []:
        rate_field = (item.get("valueObject") or {}).get("Rate")
        raw_rate = _value(rate_field) if rate_field else None
        if raw_rate:
            rate = parse_rate(raw_rate.replace("%", "").strip())
            if rate not in [r for r, _ in rates]:
                rates.append((rate, rate_field))  # type: ignore[arg-type]
    if len(rates) == 1:
        fields["vat_rate"] = _read_field("vat_rate", rates[0][1], rates[0][0])
    elif rates:
        fields["vat_rate"] = _several("vat_rate", [r for r, _ in rates])
    return fields


def parse_analyze_result(result: Mapping[str, Any]) -> Analysis:
    """Turn the ``analyzeResult`` of the invoice model into pages and fields."""
    pages: list[Page] = []
    confidences: list[float] = []
    for page in result.get("pages") or []:
        lines = [line.get("content", "") for line in page.get("lines") or []]
        pages.append(Page(int(page["pageNumber"]), "\n".join(lines)))
        confidences += [w["confidence"] for w in page.get("words") or [] if "confidence" in w]
    documents = result.get("documents") or []
    spans = tuple(
        InvoiceSpan(
            pages=frozenset(
                int(region["pageNumber"])
                for region in document.get("boundingRegions") or []
                if "pageNumber" in region
            ),
            fields=_fields(document),
        )
        for document in documents
    )
    return Analysis(
        pages=tuple(pages),
        fields=spans[0].fields if spans else {},
        text_confidence=sum(confidences) / len(confidences) if confidences else None,
        invoice_count=len(documents),
        invoices=spans,
    )


class AzureDocumentIntelligence:
    """The prebuilt invoice model of one Document Intelligence resource in Max ASP's tenant."""

    name = "document_intelligence"

    def __init__(
        self,
        endpoint: str,
        api_key: SecretStr,
        api_version: str = API_VERSION,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
        max_wait_seconds: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "https":
            raise ValueError("the Document Intelligence endpoint must use https")
        self._host = parts.netloc
        self._api_version = api_version
        self._api_key = api_key
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=endpoint, transport=transport, timeout=timeout, follow_redirects=False
        )

    @classmethod
    def from_secret_store(cls, secrets: SecretStore, endpoint: str) -> "AzureDocumentIntelligence":
        return cls(endpoint, secrets.get(API_KEY_SECRET))

    def __repr__(self) -> str:
        return f"AzureDocumentIntelligence(endpoint={str(self._http.base_url)!r})"

    def close(self) -> None:
        self._http.close()

    def _headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": self._api_key.get_secret_value()}

    def read(self, data: bytes, content_type: str) -> Analysis:
        try:
            started = self._http.post(
                f"/documentintelligence/documentModels/{MODEL}:analyze",
                params={"api-version": self._api_version},
                headers={**self._headers(), "Content-Type": content_type},
                content=data,
            )
        except httpx.HTTPError as error:
            reason = type(error).__name__
            raise ReaderUnavailable(
                f"Document Intelligence could not be reached: {reason}"
            ) from None
        if started.status_code == 400 and _error_code(started) in UNREADABLE_CODES:
            raise DocumentNotReadable(f"Document Intelligence refused it: {_error_code(started)}")
        if started.status_code != 202:
            raise ReaderUnavailable(
                f"Document Intelligence answered with status {started.status_code}"
            )
        location = started.headers.get("Operation-Location", "")
        # The key goes only to our own resource, never to a host a response names.
        parts = urlsplit(location)
        if parts.scheme != "https" or parts.netloc != self._host:
            raise ReaderUnavailable("Document Intelligence named an unexpected result address")
        result = self._wait(location)
        self._forget(location)
        analysis = parse_analyze_result(result)
        log.info("Document Intelligence read %d pages", len(analysis.pages))
        return analysis

    def _wait(self, location: str) -> Mapping[str, Any]:
        waited = 0.0
        while True:
            try:
                response = self._http.get(location, headers=self._headers())
                body = response.json() if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError) as error:
                reason = type(error).__name__
                raise ReaderUnavailable(f"Document Intelligence result failed: {reason}") from None
            if not isinstance(body, dict):
                raise ReaderUnavailable(
                    f"Document Intelligence result answered with status {response.status_code}"
                )
            status = body.get("status")
            if status == "succeeded":
                result = body.get("analyzeResult")
                if not isinstance(result, dict):
                    raise ReaderUnavailable("Document Intelligence sent no result")
                return result
            if status == "failed":
                code = _inner_code(body.get("error"))
                if code in UNREADABLE_CODES:
                    raise DocumentNotReadable(f"Document Intelligence could not read it: {code}")
                raise ReaderUnavailable(f"Document Intelligence failed: {code}")
            if waited >= self._max_wait:
                raise ReaderUnavailable("Document Intelligence took too long")
            pause = _retry_after(response)
            self._sleep(pause)
            waited += pause

    def _forget(self, location: str) -> None:
        """Delete the result at once instead of leaving it in the service for 24 hours (NF-03).
        A failure here does not stop the reading."""
        try:
            response = self._http.delete(location, headers=self._headers())
        except httpx.HTTPError:
            log.warning("Could not delete a Document Intelligence result")
            return
        if not response.is_success:
            log.warning(
                "Could not delete a Document Intelligence result: status %d", response.status_code
            )


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(max(float(response.headers.get("Retry-After", "1")), 0.5), 10.0)
    except ValueError:
        return 1.0


def _inner_code(error: Any) -> str | None:
    """The most specific error code: Document Intelligence nests them as ``innererror``."""
    code = None
    while isinstance(error, dict):
        if isinstance(error.get("code"), str):
            code = error["code"]
        error = error.get("innererror")
    return code


def _error_code(response: httpx.Response) -> str | None:
    try:
        return _inner_code(response.json().get("error"))
    except (ValueError, AttributeError):
        return None


class RecordedDocumentIntelligence:
    """The stand in for CI: answers each document with the result recorded for its SHA-256
    fingerprint, in the format the real service sends. A document without a recording is an
    error, so a test can never pass by accident."""

    name = "document_intelligence"

    def __init__(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        self._results = dict(results)

    @classmethod
    def from_directory(cls, directory: Path) -> "RecordedDocumentIntelligence":
        results = {}
        for path in sorted(directory.glob("*.json")):
            recording = json.loads(path.read_text(encoding="utf-8"))
            results[recording["sha256"]] = recording
        return cls(results)

    def read(self, data: bytes, content_type: str) -> Analysis:
        recording = self._results.get(fingerprint(data))
        if recording is None:
            raise LookupError("no recorded Document Intelligence result for this document")
        if recording.get("status") == "failed":
            raise DocumentNotReadable(
                f"Document Intelligence could not read it: {_inner_code(recording.get('error'))}"
            )
        return parse_analyze_result(recording["analyzeResult"])
