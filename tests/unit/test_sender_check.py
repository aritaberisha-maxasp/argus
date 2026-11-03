"""A sender counts as the invoicing agent or a known partner only when the email really comes from
that domain (#70, ADR 0006 point 4).

The headers below are written the way Exchange Online writes them. Covers: IN-04, IN-09, AR-02
"""

import json
from dataclasses import replace

import pytest

from argus.intake.email_text import EmailReader
from argus.reading.local import LocalReader
from argus.sorting import (
    Attachment,
    Category,
    EmailToSort,
    ModelAnswer,
    ModelAnswerInvalid,
    Route,
    SortingDecision,
    sort_email,
)
from argus.sorting.checks import check_email, check_sender
from argus.sorting.sender import authenticate, first_headers, parse_results
from tests.unit.sorting_helpers import fixture_config, passes_for

CONFIG = fixture_config()
VENDOR = "rechnungen@muster-buerobedarf.example"
AGENT = "rechnungen@maxasp.example"
CUSTOMER = "einkauf@beispiel-handel.example"

# What Exchange Online writes when SPF, DKIM and DMARC all fail.
FAILS = (
    "spf=fail (sender IP is 192.0.2.66) smtp.mailfrom={d}; dkim=none (message not signed) "
    "header.d=none;dmarc=fail action=quarantine header.from={d};compauth=fail reason=000"
)

VENDOR_INVOICE = """Muster Bürobedarf GmbH
Papierweg 12, 70001 Musterstadt, DE
USt-IdNr. DE012345601
Max ASP GmbH
Testallee 1
Rechnung
Nummer: BB-1
Gesamtbetrag 11,90 EUR
"""
OUR_INVOICE = """Max ASP GmbH
Testallee 1, 70000 Musterstadt, DE
USt-IdNr. DE099000001
Beispiel Handel GmbH
Marktstraße 10
Rechnung
Nummer: MA-1
Gesamtbetrag 119,00 EUR
"""


class FixedModel:
    def __init__(self, category: Category, confidence: float = 0.99) -> None:
        self.answer = ModelAnswer(
            category=category, confidence=confidence, invoices=(), evidence=()
        )

    def propose(self, email: EmailToSort) -> ModelAnswer:
        return self.answer


def email_from(
    address: str,
    results: str | None,
    *,
    text: str | None = VENDOR_INVOICE,
    name: str = "Muster Bürobedarf GmbH",
) -> EmailToSort:
    return EmailToSort(
        message_id="<s@argus.example>",
        sender_address=address,
        sender_name=name,
        subject="Rechnung",
        body="Anbei unsere Rechnung.",
        headers={} if results is None else {"authentication-results": results},
        attachments=(Attachment("Rechnung.pdf", "application/pdf", text),) if text else (),
    )


def domain(address: str) -> str:
    return address.rpartition("@")[2]


# AC: known only on a DMARC pass, or an SPF or DKIM pass aligned with the From domain


@pytest.mark.req("IN-04", "IN-09")
@pytest.mark.parametrize(
    ("results", "passed_by"),
    [
        (passes_for(VENDOR), "dmarc"),
        ("spf=none smtp.mailfrom=muster-buerobedarf.example; dkim=none header.d=none;"
         "dmarc=pass action=none header.from=muster-buerobedarf.example", "dmarc"),
        ("spf=fail smtp.mailfrom=muster-buerobedarf.example; dkim=pass (signature was verified) "
         "header.d=muster-buerobedarf.example;dmarc=none action=none "
         "header.from=muster-buerobedarf.example", "dkim"),
        ("spf=pass (sender IP is 192.0.2.10) smtp.mailfrom=bounce@muster-buerobedarf.example; "
         "dkim=none (message not signed) header.d=none;dmarc=none action=none "
         "header.from=muster-buerobedarf.example", "spf"),
        # The standard layout with the name of the checking server first (RFC 8601).
        ('mx.maxasp.example; spf=neutral smtp.mailfrom=other.example; '
         'dkim=pass header.d="Muster-Buerobedarf.example"', "dkim"),
    ],
)  # fmt: skip
def test_the_sender_is_known_when_the_from_domain_passes(results: str, passed_by: str) -> None:
    check = check_sender(email_from(VENDOR, results), CONFIG)
    assert (check.claimed, check.treated_as) == ("known_vendor", "known_vendor")
    assert check.authentication.passed_by == passed_by


@pytest.mark.req("IN-04", "IN-09")
@pytest.mark.parametrize(
    "results",
    [
        None,  # no header at all
        "",
        FAILS.format(d="muster-buerobedarf.example"),
        # Microsoft's guess when the domain has no DMARC policy is not a pass.
        "spf=none smtp.mailfrom=muster-buerobedarf.example; dkim=none header.d=none;"
        "dmarc=bestguesspass action=none header.from=muster-buerobedarf.example",
        "spf=softfail smtp.mailfrom=muster-buerobedarf.example; dkim=fail (body hash did not "
        "verify) header.d=muster-buerobedarf.example;dmarc=fail header.from=muster-buerobedarf"
        ".example",
        # A pass for another From domain, for example a second From header Exchange read.
        "dmarc=pass action=none header.from=attacker.invalid",
        # A pass that names no domain at all.
        "spf=pass; dkim=pass; dmarc=pass",
        "dkim=temperror header.d=muster-buerobedarf.example",
    ],
)
def test_the_sender_is_unknown_when_nothing_passes_for_the_from_domain(results: str | None) -> None:
    check = check_sender(email_from(VENDOR, results), CONFIG)
    assert (check.claimed, check.treated_as) == ("known_vendor", "unknown")
    assert check.authentication.passed_by is None


# AC: an SPF pass for a different envelope sender does not make the From address known


@pytest.mark.req("IN-04", "IN-09")
@pytest.mark.parametrize(
    "results",
    [
        "spf=pass (sender IP is 192.0.2.66) smtp.mailfrom=attacker.invalid; dkim=none "
        "(message not signed) header.d=none;dmarc=fail action=none "
        "header.from=muster-buerobedarf.example",
        "spf=pass smtp.mailfrom=bounce@attacker.invalid; dkim=pass header.d=attacker.invalid",
        # A sub or parent domain on its own is not the From domain; DMARC decides that.
        "spf=pass smtp.mailfrom=mail.muster-buerobedarf.example; dkim=pass header.d=example",
    ],
)
def test_a_pass_for_another_domain_does_not_make_the_from_address_known(results: str) -> None:
    check = check_sender(email_from(VENDOR, results), CONFIG)
    assert check.treated_as == "unknown"
    model = FixedModel(Category.INCOMING_INVOICE)
    decision = sort_email(email_from(VENDOR, results), model, CONFIG)
    assert decision.route == Route.MANUAL_REVIEW
    assert "the sender claims to be a known vendor" in " ".join(decision.reasons)


# AC: a look alike domain is never a match


@pytest.mark.req("IN-04", "IN-09", "AR-02")
@pytest.mark.parametrize(
    "address",
    [
        "rechnungen@muster-buerobedarf.example.invalid",
        "rechnungen@muster-buer0bedarf.example",
        "rechnungen@muster-buerobedarf-rechnung.example",
        "rechnungen@billing.muster-buerobedarf.example",
        "rechnungen@muster-bürobedarf.example",
        "rechnungen@xn--muster-brobedarf-4vb.example",
        "rechnungen@maxasp.example.invalid",
        "rechnungen@max-asp.example",
        "rechnungen@maxasp.examp1e",
    ],
)
def test_a_look_alike_domain_is_never_a_match(address: str) -> None:
    # The look alike domain passes every check for itself, and the display name is the
    # vendor's. It is still an unknown sender.
    email = email_from(address, passes_for(address))
    check = check_sender(email, CONFIG)
    assert (check.claimed, check.treated_as) == ("unknown", "unknown")
    assert check.authentication.passed  # its own domain is real, it is just not ours


@pytest.mark.req("IN-04", "AR-02")
def test_a_look_alike_of_the_invoicing_agent_cannot_book_a_max_asp_invoice() -> None:
    fake = "rechnungen@maxasp.example.invalid"
    decision = sort_email(
        email_from(fake, passes_for(fake), text=OUR_INVOICE, name="Max ASP Rechnungsagent"),
        FixedModel(Category.OUTGOING_INVOICE),
        CONFIG,
    )
    assert decision.route == Route.MANUAL_REVIEW
    assert decision.check_category is None


@pytest.mark.req("IN-04")
def test_the_display_name_never_makes_a_sender_known() -> None:
    address = "billing@pay-invoice-now.invalid"
    email = email_from(address, passes_for(address), name="Muster Bürobedarf GmbH")
    assert check_sender(email, CONFIG).claimed == "unknown"


@pytest.mark.req("IN-04")
def test_upper_case_in_the_address_or_the_header_still_matches() -> None:
    email = email_from(
        "Rechnungen@Muster-Buerobedarf.Example",
        "DMARC=PASS action=none header.from=MUSTER-BUEROBEDARF.EXAMPLE.",
    )
    assert check_sender(email, CONFIG).treated_as == "known_vendor"


# AC: a claim to be the invoicing agent that fails the check always goes to manual review


@pytest.mark.req("AR-02", "IN-09")
@pytest.mark.parametrize(
    ("text", "category"),
    [
        (OUR_INVOICE, Category.OUTGOING_INVOICE),
        (VENDOR_INVOICE, Category.INCOMING_INVOICE),
        (None, Category.OTHER),
    ],
)
def test_a_failed_claim_to_be_the_invoicing_agent_always_goes_to_review(
    text: str | None, category: Category
) -> None:
    for results in (None, FAILS.format(d="maxasp.example"), "spf=pass smtp.mailfrom=x.invalid"):
        email = email_from(AGENT, results, text=text, name="Max ASP Rechnungsagent")
        decision = sort_email(email, FixedModel(category, 0.999), CONFIG)
        assert decision.route == Route.MANUAL_REVIEW
        assert decision.reasons[0].startswith("our checks name no category: the sender claims")
        assert "the invoicing agent" in decision.reasons[0]


@pytest.mark.req("AR-01", "AR-02")
def test_the_real_invoicing_agent_still_goes_to_the_outgoing_stream() -> None:
    email = email_from(AGENT, passes_for(AGENT), text=OUR_INVOICE, name="Max ASP Rechnungsagent")
    decision = sort_email(email, FixedModel(Category.OUTGOING_INVOICE), CONFIG)
    assert decision.route == Route.OUTGOING_BOOKING


@pytest.mark.req("IN-09")
def test_a_failed_claim_to_be_a_known_customer_goes_to_review() -> None:
    notice = replace(
        email_from(CUSTOMER, FAILS.format(d=domain(CUSTOMER)), text=None),
        subject="Neue Lieferadresse",
        body="Bitte beachten Sie unsere neue Lieferadresse ab November.",
    )
    assert check_email(notice, CONFIG).category is None
    authentic = replace(notice, headers={"authentication-results": passes_for(CUSTOMER)})
    assert check_email(authentic, CONFIG).category == Category.OTHER


# AC: an unknown sender with an invoice goes to review if anything else is in doubt


@pytest.mark.req("IN-04", "IN-09")
def test_an_unknown_sender_with_an_invoice_and_a_doubt_goes_to_review() -> None:
    address = "auftrag@beispiel-druckerei.example"
    proforma = VENDOR_INVOICE.replace("Rechnung\n", "Proforma Rechnung\n")
    email = email_from(address, passes_for(address), text=proforma)
    decision = sort_email(email, FixedModel(Category.INCOMING_INVOICE), CONFIG)
    assert decision.route == Route.MANUAL_REVIEW
    assert decision.sender is not None
    assert decision.sender.treated_as == "unknown"


@pytest.mark.req("IN-04", "IN-09")
def test_an_unknown_sender_with_an_unreadable_invoice_goes_to_review() -> None:
    address = "billing@pay-invoice-now.invalid"
    email = email_from(address, FAILS.format(d=domain(address)), text="kaum lesbar")
    decision = sort_email(email, FixedModel(Category.INCOMING_INVOICE), CONFIG)
    assert decision.route == Route.MANUAL_REVIEW


@pytest.mark.req("IN-04", "IN-07")
def test_an_unknown_sender_with_a_clean_invoice_to_max_asp_still_goes_to_processing() -> None:
    # A new vendor is not a doubt by itself; the vendor checks later catch it (VM-03, PR-05).
    address = "auftrag@beispiel-druckerei.example"
    email = email_from(address, passes_for(address))
    decision = sort_email(email, FixedModel(Category.INCOMING_INVOICE), CONFIG)
    assert decision.route == Route.INVOICE_PROCESSING


# Only the topmost header counts: the one Exchange Online wrote


def raw_email(*results: str, sender: str = AGENT) -> bytes:
    headers = "".join(f"Authentication-Results: {r}\r\n" for r in results)
    return (
        f"{headers}From: Max ASP Rechnungsagent <{sender}>\r\nTo: accounts@maxasp.example\r\n"
        "Subject: Rechnung MA-1\r\nMessage-ID: <raw@argus.example>\r\n\r\nAnbei die Rechnung.\r\n"
    ).encode()


@pytest.mark.req("IN-04", "AR-02")
def test_a_header_the_sender_wrote_below_the_one_from_exchange_is_ignored() -> None:
    forged = passes_for(AGENT)
    exchange = FAILS.format(d="maxasp.example")
    email = EmailReader(LocalReader())(raw_email(exchange, forged))
    assert email.headers["authentication-results"] == exchange
    assert check_sender(email, CONFIG).treated_as == "unknown"
    # The other way round, the topmost pass is the one that counts.
    email = EmailReader(LocalReader())(raw_email(forged, exchange))
    assert check_sender(email, CONFIG).treated_as == "invoicing_agent"


@pytest.mark.req("IN-04")
def test_first_headers_keeps_the_topmost_copy_of_each_header() -> None:
    items = [("Received", "b"), ("Authentication-Results", "top"), ("received", "a")]
    assert first_headers(items) == {"received": "b", "authentication-results": "top"}


# Reading the header


@pytest.mark.req("IN-04")
def test_comments_and_quotes_in_the_header_cannot_smuggle_in_a_pass() -> None:
    sneaky = (
        "spf=fail (dmarc=pass header.from=muster-buerobedarf.example) smtp.mailfrom=x.invalid; "
        'dkim=fail header.d="muster-buerobedarf.example; dmarc=pass"'
    )
    check = check_sender(email_from(VENDOR, sneaky), CONFIG)
    assert check.treated_as == "unknown"
    nested = "dkim=pass (a (nested) comment) header.d=muster-buerobedarf.example"
    assert check_sender(email_from(VENDOR, nested), CONFIG).treated_as == "known_vendor"


@pytest.mark.req("IN-04")
def test_the_header_is_read_into_one_result_per_method() -> None:
    [spf, dkim, dmarc, compauth] = parse_results(passes_for(VENDOR))
    assert (spf.method, spf.result, spf.properties["smtp.mailfrom"]) == (
        "spf",
        "pass",
        "muster-buerobedarf.example",
    )
    assert (dkim.method, dkim.properties["header.d"]) == ("dkim", "muster-buerobedarf.example")
    assert (dmarc.method, dmarc.properties["action"]) == ("dmarc", "none")
    assert (compauth.method, compauth.result) == ("compauth", "pass")


@pytest.mark.req("IN-04")
def test_a_from_address_without_a_domain_never_passes() -> None:
    assert not authenticate("", {"authentication-results": "dmarc=pass header.from="}).passed
    assert not authenticate("nobody", {"authentication-results": passes_for(VENDOR)}).passed


# AC: the result is stored with the email and in the audit log


@pytest.mark.req("IN-04", "NF-01", "NF-03")
def test_the_check_result_is_in_the_audit_details_with_domains_only() -> None:
    email = email_from(
        AGENT,
        "spf=pass smtp.mailfrom=erika.mustermann@attacker.invalid; dkim=none header.d=none;"
        "dmarc=fail header.from=maxasp.example",
        text=OUR_INVOICE,
    )
    decision = sort_email(email, FixedModel(Category.OUTGOING_INVOICE), CONFIG)
    details = decision.audit_details()
    assert details["sender"] == {
        "claimed": "invoicing_agent",
        "treated_as": "unknown",
        "from_domain": "maxasp.example",
        "authenticated": False,
        "passed_by": None,
        "results": [
            "spf=pass attacker.invalid",
            "dkim=none none",
            "dmarc=fail maxasp.example",
        ],
    }
    # No address, so no name of a person, reaches the audit log (NF-03).
    written = json.dumps(details)
    assert "rechnungen@" not in written
    assert "erika.mustermann" not in written


@pytest.mark.req("IN-04", "NF-01")
def test_the_check_result_comes_back_from_the_recorded_decision() -> None:
    email = email_from(VENDOR, passes_for(VENDOR))
    decision = sort_email(email, FixedModel(Category.INCOMING_INVOICE), CONFIG)
    assert decision.sender is not None
    again = SortingDecision.from_audit_details(json.loads(json.dumps(decision.audit_details())))
    assert again == decision
    assert again.sender == decision.sender
    # A decision recorded before #70 has no sender check and still reads back.
    older = {**decision.audit_details(), "sender": None}
    assert SortingDecision.from_audit_details(older).sender is None


@pytest.mark.req("IN-04")
def test_a_model_answer_that_cannot_be_used_still_keeps_the_sender_check() -> None:
    class Broken:
        def propose(self, email: EmailToSort) -> ModelAnswer:
            raise ModelAnswerInvalid("not JSON")

    decision = sort_email(email_from(VENDOR, passes_for(VENDOR)), Broken(), CONFIG)
    assert decision.route == Route.MANUAL_REVIEW
    assert decision.audit_details()["sender"]["treated_as"] == "known_vendor"
