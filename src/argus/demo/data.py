"""The emails the demo starts with (#124). Every one is a synthetic fixture."""

import os
from dataclasses import dataclass
from pathlib import Path

# Where the fixture emails are. In the argus-demo image they sit in /app/tests/fixtures/emails
# and the working directory is /app, so the default works there and in a checkout.
EMAILS_ENV = "ARGUS_DEMO_EMAILS"
DEFAULT_EMAILS = Path("tests") / "fixtures" / "emails"


@dataclass(frozen=True)
class DemoEmail:
    file: str
    shows: str


# One email for each thing the demo shows, in the order they arrive. The duplicate comes after
# the invoice it repeats. tests/fixtures/emails/manifest.csv has what ARGUS should do with each.
DEMO_EMAILS: tuple[DemoEmail, ...] = (
    DemoEmail("E01_buerobedarf-rechnung.eml", "incoming invoice as a PDF"),
    DemoEmail("E07_reinigung-scan.eml", "incoming invoice as a paper scan"),
    DemoEmail("E08_cloud-hosting-xrechnung.eml", "incoming XRechnung"),
    DemoEmail("E09_buerobedarf-zugferd.eml", "incoming ZUGFeRD invoice"),
    DemoEmail("E17_ausgang-inland.eml", "outgoing invoice of Max ASP"),
    DemoEmail("E25_zahlungserinnerung.eml", "payment reminder"),
    DemoEmail("E15_doppelte-rechnung.eml", "the invoice of E01 sent a second time"),
    DemoEmail("E49_falscher-empfaenger.eml", "invoice that fails §14 UStG: wrong recipient"),
    DemoEmail("E43_spam-fake-invoice.eml", "doubtful sender: SPF, DKIM and DMARC fail"),
    DemoEmail("E32_newsletter-bekannter-lieferant.eml", "marketing mail from a known supplier"),
)


def emails_folder(given: str | None = None) -> Path:
    return Path(given or os.environ.get(EMAILS_ENV) or DEFAULT_EMAILS)
