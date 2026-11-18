"""Which vendor account an invoice belongs to (#35, PR-02, VM-03).

The rules run on the synthetic fixtures: the vendor accounts the fake DATEV serves and the
values ``tests/fixtures/documents/expected.json`` says each fixture invoice carries. The made
up vendors below are synthetic as well (CLAUDE.md).
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from argus.datev import Account, BankAccount, DatevConnectGateway
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev
from argus.intake.vendors import (
    InvoiceParty,
    Match,
    MatchedBy,
    NoMatch,
    NoMatchReason,
    VendorBook,
    masked_iban,
    match_vendor,
)

DOCUMENTS = Path(__file__).resolve().parents[2] / "fixtures" / "documents"
EXPECTED = json.loads((DOCUMENTS / "expected.json").read_text(encoding="utf-8"))
INCOMING = [d for d in EXPECTED if d["direction"] == "incoming"]
OWN_VAT_ID = "DE099000001"  # Max ASP's synthetic VAT ID in the fixtures
CUSTOMER_VAT_ID = "DE012345701"  # Beispiel Handel GmbH, customer 10001


@pytest.fixture(scope="module")
def book() -> Iterator[VendorBook]:
    with TestClient(create_fake_datev()) as fake:
        yield VendorBook.from_datev(DatevConnectGateway(fake, DEFAULT_CLIENT_ID), [OWN_VAT_ID])


def party_of(document: dict[str, Any]) -> InvoiceParty:
    return InvoiceParty(
        vat_id=document["vendor_vat_id"],
        iban=document["iban_on_document"],
        name=document["issuer_name"],
    )


def vendor(number: int, name: str, vat_id: str | None, *ibans: str) -> Account:
    return Account(
        number=number,
        name=name,
        vat_id=vat_id,
        bank_accounts=tuple(BankAccount(iban=i) for i in ibans),
    )


@pytest.mark.req("PR-02")
@pytest.mark.parametrize(
    "document",
    [d for d in INCOMING if d["counterparty_account"] and d["document_id"] != "D06"],
    ids=lambda d: d["document_id"],
)
def test_every_fixture_invoice_from_a_known_vendor_matches_by_vat_id_and_iban(
    document: dict[str, Any], book: VendorBook
) -> None:
    found = match_vendor(party_of(document), book)

    assert isinstance(found, Match)
    assert found.by is MatchedBy.VAT_ID_AND_IBAN
    assert str(found.vendor.number) == document["counterparty_account"]
    assert not MatchedBy.VAT_ID_AND_IBAN.weaker


@pytest.mark.req("PR-02", "PR-05")
def test_a_changed_iban_still_finds_the_vendor_by_vat_id_and_leaves_the_block_to_36(
    book: VendorBook,
) -> None:
    d06 = next(d for d in EXPECTED if d["document_id"] == "D06")

    found = match_vendor(party_of(d06), book)

    assert isinstance(found, Match)
    assert (found.vendor.number, found.by) == (70006, MatchedBy.VAT_ID)


@pytest.mark.req("VM-03")
@pytest.mark.parametrize(
    "document",
    [d for d in INCOMING if not d["counterparty_account"]],
    ids=lambda d: d["document_id"],
)
def test_a_fixture_invoice_from_an_unknown_vendor_matches_nothing(
    document: dict[str, Any], book: VendorBook
) -> None:
    assert match_vendor(party_of(document), book) == NoMatch(NoMatchReason.NOT_FOUND)


@pytest.mark.req("PR-02")
def test_without_a_vat_id_the_iban_finds_the_vendor(book: VendorBook) -> None:
    party = InvoiceParty(iban="DE44 0000 0002 0000 7000 01", name="Someone Else")

    found = match_vendor(party, book)

    assert isinstance(found, Match)
    assert (found.vendor.number, found.by) == (70001, MatchedBy.IBAN)


@pytest.mark.req("PR-02")
def test_an_iban_match_against_a_different_vat_id_fails(book: VendorBook) -> None:
    # DATEV holds the vendor's VAT ID, and the invoice states another one: no match by IBAN.
    party = InvoiceParty(vat_id="DE999999991", iban="DE44000000020000700001")

    assert match_vendor(party, book) == NoMatch(NoMatchReason.VAT_ID_DIFFERS, (70001,))


@pytest.mark.req("PR-02")
def test_a_vendor_without_a_vat_id_in_datev_is_found_by_iban() -> None:
    book = VendorBook((vendor(70101, "Muster Kiosk", None, "DE02000000000000000101"),))
    party = InvoiceParty(vat_id="DE999999992", iban="DE02000000000000000101")

    found = match_vendor(party, book)

    assert isinstance(found, Match)
    assert (found.vendor.number, found.by) == (70101, MatchedBy.IBAN)


@pytest.mark.req("PR-02")
def test_a_name_only_match_is_marked_weaker(book: VendorBook) -> None:
    party = InvoiceParty(name="MUSTER  BÜROBEDARF gmbh")

    found = match_vendor(party, book)

    assert isinstance(found, Match)
    assert (found.vendor.number, found.by) == (70001, MatchedBy.NAME)
    assert found.by.weaker


@pytest.mark.req("PR-02")
def test_a_name_match_against_a_different_vat_id_fails(book: VendorBook) -> None:
    party = InvoiceParty(vat_id="DE999999993", name="Muster Bürobedarf GmbH")

    assert match_vendor(party, book) == NoMatch(NoMatchReason.VAT_ID_DIFFERS, (70001,))


@pytest.mark.req("PR-02")
def test_umlauts_written_out_still_match_by_name(book: VendorBook) -> None:
    found = match_vendor(InvoiceParty(name="Muster Buerobedarf GmbH"), book)

    assert isinstance(found, Match)
    assert found.vendor.number == 70001


@pytest.mark.req("PR-02", "VM-03")
@pytest.mark.parametrize(
    "party",
    [
        InvoiceParty(vat_id="DE012345681"),
        InvoiceParty(iban="DE02000000000000000202"),
        InvoiceParty(name="Muster Doppel GmbH"),
    ],
    ids=["vat_id", "iban", "name"],
)
def test_two_vendor_accounts_that_fit_equally_well_match_neither(party: InvoiceParty) -> None:
    book = VendorBook(
        (
            vendor(70201, "Muster Doppel GmbH", "DE012345681", "DE02000000000000000202"),
            vendor(70202, "Muster Doppel GmbH", "DE012345681", "DE02000000000000000202"),
        )
    )

    assert match_vendor(party, book) == NoMatch(NoMatchReason.SEVERAL_VENDORS, (70201, 70202))


@pytest.mark.req("PR-02")
def test_of_two_accounts_with_one_vat_id_the_iban_picks_the_right_one() -> None:
    book = VendorBook(
        (
            vendor(70301, "Muster Zwei GmbH", "DE012345682", "DE02000000000000000301"),
            vendor(70302, "Muster Zwei GmbH Filiale", "DE012345682", "DE02000000000000000302"),
        )
    )
    party = InvoiceParty(vat_id="DE012345682", iban="DE02000000000000000302")

    found = match_vendor(party, book)

    assert isinstance(found, Match)
    assert (found.vendor.number, found.by) == (70302, MatchedBy.VAT_ID_AND_IBAN)


@pytest.mark.req("PR-02")
def test_max_asps_own_vat_id_never_matches_and_nothing_weaker_is_tried(book: VendorBook) -> None:
    # The PO note on #35: the vendor match must fail on Max ASP's own VAT ID (#106).
    party = InvoiceParty(
        vat_id="DE 099 000 001", iban="DE44000000020000700001", name="Muster Bürobedarf GmbH"
    )

    assert match_vendor(party, book) == NoMatch(NoMatchReason.OWN_VAT_ID)


@pytest.mark.req("PR-02")
def test_a_customers_vat_id_never_matches_and_nothing_weaker_is_tried(book: VendorBook) -> None:
    # The PO note on #35: a customer's VAT ID read off the address block must not match.
    party = InvoiceParty(
        vat_id=CUSTOMER_VAT_ID, iban="DE44000000020000700001", name="Muster Bürobedarf GmbH"
    )

    assert match_vendor(party, book) == NoMatch(NoMatchReason.CUSTOMER_VAT_ID)


@pytest.mark.req("PR-02")
def test_a_customer_who_is_also_a_vendor_matches_only_when_the_iban_confirms_it() -> None:
    both = vendor(70401, "Muster Partner GmbH", "DE012345683", "DE02000000000000000401")
    book = VendorBook((both,), customer_vat_ids=frozenset({"DE012345683"}))

    confirmed = match_vendor(InvoiceParty("DE012345683", "DE02000000000000000401"), book)
    alone = match_vendor(InvoiceParty("DE012345683", "DE02000000000000000999"), book)

    assert confirmed == Match(both, MatchedBy.VAT_ID_AND_IBAN)
    assert alone == NoMatch(NoMatchReason.CUSTOMER_VAT_ID)


@pytest.mark.req("PR-02")
def test_nothing_read_matches_nothing(book: VendorBook) -> None:
    assert match_vendor(InvoiceParty(), book) == NoMatch(NoMatchReason.NOT_FOUND)


@pytest.mark.req("PR-02")
def test_flagged_values_are_not_used_to_find_the_vendor() -> None:
    reading = {
        "fields": {
            "vendor_vat_id": {"value": "DE 012 345 6O1", "flag": None},
            "iban_on_document": {"value": "DE44000000020000700001", "flag": "from_model"},
            "issuer_name": {"value": "Muster Bürobedarf GmbH", "flag": "low_confidence"},
        }
    }

    assert InvoiceParty.from_reading(reading) == InvoiceParty(vat_id="DE012345601")
    assert InvoiceParty.from_reading({}) == InvoiceParty()


def test_an_iban_shown_to_people_is_masked() -> None:
    assert masked_iban("DE44000000020000700001") == "DE ... 0001"
    assert masked_iban("DE1") == "an IBAN"
