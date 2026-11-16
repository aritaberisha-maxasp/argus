"""The emails the demo starts with (#124). Every one is a synthetic fixture."""

import os
import re
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


# Demo only emails sit in this subfolder of the emails folder, so the 50 test emails of AC-1
# (tests/fixtures/emails/manifest.csv) stay exactly 50 and the tests that serve them all do not
# pick these up.
DEMO_ONLY = "demo"

# One email for each thing the demo shows, in the order they arrive. The duplicate comes after
# the invoice it repeats. tests/fixtures/emails/manifest.csv has what ARGUS should do with each
# test email; the demo only ones say it in their ``shows``.
DEMO_EMAILS: tuple[DemoEmail, ...] = (
    DemoEmail("E01_buerobedarf-rechnung.eml", "incoming invoice as a PDF"),
    DemoEmail("E07_reinigung-scan.eml", "incoming invoice as a paper scan"),
    DemoEmail("E08_cloud-hosting-xrechnung.eml", "incoming XRechnung"),
    DemoEmail("E09_buerobedarf-zugferd.eml", "incoming ZUGFeRD invoice"),
    DemoEmail("E17_ausgang-inland.eml", "outgoing invoice of Max ASP"),
    DemoEmail("E25_zahlungserinnerung.eml", "payment reminder"),
    DemoEmail("E15_doppelte-rechnung.eml", "the invoice of E01 sent a second time"),
    DemoEmail("E49_falscher-empfaenger.eml", "invoice that fails §14 UStG: wrong recipient"),
    DemoEmail(
        f"{DEMO_ONLY}/E51_rechnung-ohne-waehrung.eml",
        "invoice whose amounts carry no currency: a person enters amount and currency",
    ),
    DemoEmail("E43_spam-fake-invoice.eml", "doubtful sender: SPF, DKIM and DMARC fail"),
    DemoEmail("E32_newsletter-bekannter-lieferant.eml", "marketing mail from a known supplier"),
)


def emails_folder(given: str | None = None) -> Path:
    return Path(given or os.environ.get(EMAILS_ENV) or DEFAULT_EMAILS)


# The test emails are named E01_<slug>.eml to E50_<slug>.eml, the demo only ones from E51 on.
# A person picks one by its number.
EMAIL_ID = re.compile(r"E\d{2}")


def chosen_email(folder: Path, email_id: str) -> DemoEmail:
    """The one test email a person picked by its number, for example ``E12`` (#136), from the
    test emails or the demo only ones."""
    wanted = email_id.strip().upper()
    if not EMAIL_ID.fullmatch(wanted):
        raise ValueError(f"{email_id!r} is not a test email number like E12")
    found = sorted(
        path.relative_to(folder).as_posix()
        for where in (folder, folder / DEMO_ONLY)
        for path in where.glob(f"{wanted}_*.eml")
        if path.is_file()
    )
    if len(found) != 1:
        raise LookupError(f"no single test email {wanted} in {folder}")
    return DemoEmail(found[0], "chosen test email")
