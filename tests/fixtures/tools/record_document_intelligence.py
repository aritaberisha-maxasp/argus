"""Write the recorded Document Intelligence answers the CI stand in serves (#29).

Usage, from the repository root:
    python3 tests/fixtures/tools/record_document_intelligence.py

Writes tests/fixtures/document-intelligence/<document>.json in the format of the
prebuilt-invoice model, API version 2024-11-30: the status, the analyzeResult with the
text of each page, and the invoice fields with a confidence each.

These are written by hand from expected.json, not recorded from Azure, because the
Azure setup (#65) does not exist yet. Once it does, replace them with real recordings
of the same documents and run the tests again. The page text is the PDF text layer;
for the scans it is the reading in tests/fixtures/readings, or Tesseract.

Some answers leave a field out or give it a low confidence on purpose, as the real
service does, so the tests see ARGUS fill it from the page or flag it.
"""

import hashlib
import io
import json
import subprocess
from pathlib import Path

from pypdf import PdfReader

FIXTURES = Path(__file__).resolve().parents[1]
DOCUMENTS = FIXTURES / "documents"
READINGS = FIXTURES / "readings"
OUT = FIXTURES / "document-intelligence"

# The service misses things too. Fields left out per document, to test the page text.
MISSED = {
    "D04": {"VendorTaxId", "ServiceStartDate", "ServiceEndDate"},
    "D11": {"VendorTaxId", "PaymentDetails"},
    "D22": {"CustomerName", "TaxDetails"},
}
# A field read with a low confidence, to test the flag. The value stays right.
LOW = {"D07": {"InvoiceTotal": 0.71, "VendorTaxId": 0.64}}
SCAN_CONFIDENCE = 0.92


def page_lines(path: Path) -> list[list[str]]:
    if path.suffix == ".pdf":
        return [(p.extract_text() or "").splitlines() for p in PdfReader(path).pages]
    reading = READINGS / f"{path.name.split('_', 1)[1]}.txt"
    if reading.exists():
        return [reading.read_text(encoding="utf-8").splitlines()]
    text = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", "deu+eng"], capture_output=True, check=True
    ).stdout.decode()
    return [text.splitlines()]


def text_field(value, content, confidence):
    return {"type": "string", "valueString": value, "content": content,
            "boundingRegions": [{"pageNumber": 1, "polygon": []}], "confidence": confidence}


def date_field(value, confidence):
    return {"type": "date", "valueDate": value, "content": value,
            "boundingRegions": [{"pageNumber": 1, "polygon": []}], "confidence": confidence}


def money_field(amount, currency, confidence):
    return {"type": "currency",
            "valueCurrency": {"amount": float(amount), "currencyCode": currency},
            "content": f"{amount} {currency}",
            "boundingRegions": [{"pageNumber": 1, "polygon": []}], "confidence": confidence}


def invoice_fields(doc):
    c = 0.98 if doc["format"] == "pdf" else 0.9
    fields = {
        "VendorName": text_field(doc["issuer_name"], doc["issuer_name"], c),
        "CustomerName": text_field(doc["recipient_name"], doc["recipient_name"], c),
        "VendorTaxId": text_field(doc["vendor_vat_id"], doc["vendor_vat_id"], c),
        "InvoiceId": text_field(doc["invoice_number"], doc["invoice_number"], c),
        "InvoiceDate": date_field(doc["invoice_date"], c),
        "ServiceStartDate": date_field(doc["service_period"][0], c),
        "ServiceEndDate": date_field(doc["service_period"][1], c),
        "SubTotal": money_field(doc["net_amount"], doc["currency"], c),
        "TotalTax": money_field(doc["vat_amount"], doc["currency"], c),
        "InvoiceTotal": money_field(doc["gross_amount"], doc["currency"], c),
        "TaxDetails": {"type": "array", "valueArray": [{"type": "object", "valueObject": {
            "Rate": text_field(f"{doc['vat_rate']}%", f"{doc['vat_rate']} %", c),
            "Amount": money_field(doc["vat_amount"], doc["currency"], c)}}]},
        "PaymentDetails": {"type": "array", "valueArray": [{"type": "object", "valueObject": {
            "IBAN": text_field(doc["iban_on_document"], doc["iban_on_document"], c)}}]},
    }
    if doc["due_date"]:
        fields["DueDate"] = date_field(doc["due_date"], c)
    for name in MISSED.get(doc["document_id"], ()):
        fields.pop(name)
    for name, confidence in LOW.get(doc["document_id"], {}).items():
        fields[name]["confidence"] = confidence
    return fields


def recording(path: Path, fields):
    data = path.read_bytes()
    word_confidence = 0.99 if path.suffix == ".pdf" else SCAN_CONFIDENCE
    pages = []
    for number, lines in enumerate(page_lines(path), start=1):
        lines = [line for line in lines if line.strip()]
        words = [{"content": w, "confidence": word_confidence} for line in lines for w in line.split()]
        pages.append({"pageNumber": number, "lines": [{"content": line} for line in lines],
                      "words": words})
    documents = [{"docType": "invoice", "fields": fields}] if fields is not None else []
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "file": path.name,
        "recorded": "written by hand from expected.json until the Azure setup (#65) exists",
        "status": "succeeded",
        "analyzeResult": {"apiVersion": "2024-11-30", "modelId": "prebuilt-invoice",
                          "content": "\n".join(l["content"] for p in pages for l in p["lines"]),
                          "pages": pages, "documents": documents},
    }


def main():
    OUT.mkdir(exist_ok=True)
    expected = json.loads((DOCUMENTS / "expected.json").read_text(encoding="utf-8"))
    for doc in expected:
        if doc["format"] not in ("pdf", "scan"):
            continue  # e-invoices are read from their XML (#28)
        path = DOCUMENTS / doc["files"][0]
        out = OUT / f"{doc['document_id']}.json"
        out.write_text(json.dumps(recording(path, invoice_fields(doc)), indent=1,
                                  ensure_ascii=False) + "\n", encoding="utf-8")
    # The unreadable scan of E46: the service finds almost nothing on it.
    scan = DOCUMENTS / "L46_scan_0001.jpg"
    (OUT / "L46.json").write_text(json.dumps(recording(scan, None), indent=1,
                                             ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
