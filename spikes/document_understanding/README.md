# Document understanding prototype (spike for issue #5)

Spike code. It answers whether ARGUS can classify the accounts inbox and read invoices, and how. It is not product code: the build stories rewrite it inside the product skeleton. Findings: [docs/spikes/document-understanding.md](../../docs/spikes/document-understanding.md). Decision: [ADR 0006](../../docs/adr/0006-understanding-emails-and-invoices.md).

Covers: IN-02, IN-03, IN-04, IN-08, IN-09, PR-01, PR-03, NF-02

## Files

| File | What it does |
| --- | --- |
| `einvoice.py` | Reads XRechnung (UBL and CII) and ZUGFeRD / Factur-X XML into the PR-01 fields |
| `documents.py` | Reads an `.eml`, its attachments and body, runs OCR, decides the document type, extracts and checks fields |
| `classify.py` | Points for the four IN-04 categories, confidence, safety gates, one case per invoice |
| `evaluate.py` | Runs everything on QA's 50 fixture emails or on the hold out set and writes `results/` |
| `holdout/generate.py` | Builds 16 extra synthetic emails the rules were never tuned on |
| `test_prototype.py` | Keeps the numbers honest: AC-1 on the fixtures, no invoice deleted on either set |

## Run it

From the repository root, on Linux:

```
sudo apt install tesseract-ocr tesseract-ocr-deu
python3 -m pip install -r spikes/document_understanding/requirements.txt
python3 spikes/document_understanding/evaluate.py
python3 spikes/document_understanding/evaluate.py --holdout
python3 -m pytest spikes/document_understanding -q
```

`holdout/generate.py` only needs to run again if you change the hold out emails. Do not tune the rules on the hold out set: it exists to measure how the rules do on emails they were not written for. Add new emails instead.

All data is synthetic and follows the rules in [tests/fixtures/README.md](../../tests/fixtures/README.md). Nothing is sent to any outside service.
