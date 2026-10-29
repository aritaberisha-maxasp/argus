"""Every e-invoice fixture passes the official KoSIT validator, and broken ones do not.

Covers: NF-02, IN-02

Runs against the validator container (docker/kosit-validator). Set ``ARGUS_KOSIT_URL``, for
example ``http://localhost:8090/``. Skipped locally without it; in CI it must be set.
"""

import os
from pathlib import Path

import pytest

from argus.einvoice import KositValidator, embedded_xml

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
DOCUMENTS = FIXTURES / "documents"
EINVOICES = FIXTURES / "einvoice"

XML_FIXTURES = [
    DOCUMENTS / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml",
    DOCUMENTS / "D24_Rechnung_MA-2026-0104_xrechnung.xml",
    EINVOICES / "X01_xrechnung_cii_BCH-26-11-0061.xml",
    EINVOICES / "X02_xrechnung_ubl_credit_note_GS-BB-2026-0071.xml",
]


@pytest.fixture(scope="module")
def validator() -> KositValidator:
    url = os.environ.get("ARGUS_KOSIT_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("ARGUS_KOSIT_URL must be set in CI, the e-invoice checks need it")
        pytest.skip("ARGUS_KOSIT_URL is not set, see docs/design/einvoice-reading.md")
    return KositValidator(url)


def zugferd_xml() -> bytes:
    xml = embedded_xml((DOCUMENTS / "D09_Rechnung_BB-2026-1243_zugferd.pdf").read_bytes())
    assert xml is not None
    return xml


@pytest.mark.req("NF-02")
@pytest.mark.parametrize("path", XML_FIXTURES, ids=lambda p: p.name)
def test_the_xrechnung_fixtures_pass_the_official_check(
    validator: KositValidator, path: Path
) -> None:
    result = validator.validate(path.read_bytes())

    assert result.accepted, [f"{m.code}: {m.text}" for m in result.errors]
    assert result.scenario is not None and "XRechnung" in result.scenario


@pytest.mark.req("NF-02")
def test_the_zugferd_fixture_passes_the_en16931_check(validator: KositValidator) -> None:
    result = validator.validate(zugferd_xml())

    assert result.accepted, [f"{m.code}: {m.text}" for m in result.errors]
    assert result.scenario == "EN16931 (CII)"


@pytest.mark.req("NF-02")
def test_an_xrechnung_without_buyer_reference_is_not_accepted(validator: KositValidator) -> None:
    xml = (DOCUMENTS / "D08_Rechnung_BCH-26-10-0057_xrechnung.xml").read_bytes()
    broken = xml.replace(b"<cbc:BuyerReference>n/a</cbc:BuyerReference>", b"")

    result = validator.validate(broken)

    assert not result.accepted
    assert "BR-DE-15" in {m.code for m in result.errors}


@pytest.mark.req("NF-02")
def test_an_xrechnung_whose_totals_do_not_add_up_is_not_accepted(
    validator: KositValidator,
) -> None:
    xml = (EINVOICES / "X01_xrechnung_cii_BCH-26-11-0061.xml").read_bytes()
    broken = xml.replace(
        b"<ram:GrandTotalAmount>297.50</ram:GrandTotalAmount>",
        b"<ram:GrandTotalAmount>279.50</ram:GrandTotalAmount>",
    )

    result = validator.validate(broken)

    assert not result.accepted and result.errors


@pytest.mark.req("NF-02")
def test_xml_that_is_no_einvoice_matches_no_rules(validator: KositValidator) -> None:
    result = validator.validate(b"<order><id>1</id></order>")

    assert not result.accepted
    assert result.scenario is None
