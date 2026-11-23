"""The approval request email and the checks the Graph client makes before it goes out (#191,
ADR 0010).

Covers AP-02 (the email goes to the person on the request), AP-06 (a deputy is told for whom)
and NF-03 (the email carries the vendor name, number, amount, currency and link, and nothing
else). Names and addresses are made up.
"""

import json
from dataclasses import replace

import httpx
import pytest

from argus.intake.graph import (
    ApprovalMail,
    GraphCall,
    GraphCallRefused,
    GraphClient,
    approval_amount,
    approval_body,
    approval_body_problem,
    render_approval,
)
from standins.graph.app import MAILBOX, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

pytestmark = pytest.mark.req("AP-02", "NF-03")

MAIL = ApprovalMail(
    request_id=3,
    case_id=42,
    recipient_address="alice@maxasp.example",
    recipient_name="Alice Test",
    on_behalf_of_name=None,
    vendor_name="Muster Bürobedarf GmbH",
    document_number="RE-2026-0042",
    gross_cents=123456,
    currency="EUR",
    credit_note=False,
    link="https://argus.example/approvals/42",
)
APPROVALS = frozenset({GraphCall.SEND_APPROVAL_REQUEST})


def body_bytes(body: object) -> bytes:
    return json.dumps(body).encode()


def test_the_email_holds_vendor_number_amount_currency_and_link_only() -> None:
    subject, text = render_approval(MAIL)
    assert subject == "ARGUS: please approve invoice RE-2026-0042 from Muster Bürobedarf GmbH"
    assert text == (
        "Hello Alice Test,\n\n"
        "A checked invoice is waiting for your approval in ARGUS.\n\n"
        "Vendor: Muster Bürobedarf GmbH\n"
        "Invoice number: RE-2026-0042\n"
        "Gross amount: 1,234.56 EUR\n\n"
        "Open it here to approve or reject it: https://argus.example/approvals/42\n\n"
        "The link asks you to sign in with your Microsoft account. You decide on that page only."
        " ARGUS does not read replies to this email.\n"
    )
    assert "IBAN" not in text


@pytest.mark.req("AP-06")
def test_a_deputy_is_told_for_whom_and_a_credit_note_says_so() -> None:
    _, text = render_approval(
        replace(MAIL, on_behalf_of_name="Bob Away", credit_note=True, gross_cents=-5000)
    )
    assert "You get this as the deputy of Bob Away, who is away." in text
    assert "A checked credit note is waiting" in text
    assert "Credit note number: RE-2026-0042" in text
    assert "Gross amount: -50.00 EUR" in text


def test_a_value_read_from_the_invoice_cannot_add_lines() -> None:
    subject, text = render_approval(
        replace(MAIL, vendor_name="Evil\nIBAN: DE00 1234\n\nPay now", document_number="x" * 300)
    )
    assert "Vendor: Evil IBAN: DE00 1234 Pay now\n" in text
    assert "\n\n" not in subject and len(subject) < 300


@pytest.mark.parametrize(
    ("cents", "shown"), [(0, "0.00 EUR"), (5, "0.05 EUR"), (500000, "5,000.00 EUR")]
)
def test_amounts(cents: int, shown: str) -> None:
    assert approval_amount(cents, "EUR") == shown


def test_the_stored_request_passes_its_own_check() -> None:
    assert approval_body_problem(body_bytes(approval_body(MAIL)), MAIL) is None
    # The address is compared without case, as Exchange does.
    upper = approval_body(replace(MAIL, recipient_address="ALICE@maxasp.example"))
    assert approval_body_problem(body_bytes(upper), MAIL) is None


def test_another_recipient_is_refused() -> None:
    other = approval_body(replace(MAIL, recipient_address="mallory@evil.example"))
    assert "not the person on the request" in (approval_body_problem(body_bytes(other), MAIL) or "")
    two = approval_body(MAIL)
    two["message"]["toRecipients"].append({"emailAddress": {"address": "mallory@evil.example"}})
    assert approval_body_problem(body_bytes(two), MAIL)


def test_a_copy_or_an_attachment_is_refused() -> None:
    for extra in ("ccRecipients", "bccRecipients", "attachments", "replyTo"):
        body = approval_body(MAIL)
        body["message"][extra] = [{"emailAddress": {"address": "mallory@evil.example"}}]
        assert approval_body_problem(body_bytes(body), MAIL), extra


def test_more_or_other_text_is_refused() -> None:
    body = approval_body(MAIL)
    body["message"]["body"]["content"] += "IBAN: DE89 3704 0044 0532 0130 00\n"
    assert "more or other text" in (approval_body_problem(body_bytes(body), MAIL) or "")
    body = approval_body(MAIL)
    body["message"]["subject"] = "Approve now"
    assert approval_body_problem(body_bytes(body), MAIL)
    body = approval_body(MAIL)
    body["message"]["body"]["contentType"] = "HTML"
    assert approval_body_problem(body_bytes(body), MAIL)


def test_without_a_stored_request_nothing_is_sent() -> None:
    assert approval_body_problem(body_bytes(approval_body(MAIL)), None)


def test_a_repeated_key_is_refused() -> None:
    raw = (
        b'{"message": {"subject": "s", "body": {"contentType": "Text", "content": "t"},'
        b' "toRecipients": [{"emailAddress": {"address": "alice@maxasp.example"}}]},'
        b' "message": {}}'
    )
    assert approval_body_problem(raw, MAIL)


def test_the_client_sends_the_stored_request_to_the_fake_graph() -> None:
    standin = create_app(token=TOKEN)
    graph = GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(standin),
        calls=APPROVALS,
        approval_lookup=lambda _: MAIL,
    )
    graph.send_approval_request(MAIL)
    (sent,) = standin.state.mailbox.notices
    assert sent["toRecipients"] == [{"emailAddress": {"address": "alice@maxasp.example"}}]
    assert sent["subject"] == render_approval(MAIL)[0]


def test_the_client_refuses_a_body_that_is_not_the_request_being_sent() -> None:
    reached: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(202)

    graph = GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=httpx.MockTransport(record),
        calls=APPROVALS,
        approval_lookup=lambda _: MAIL,
    )
    other = approval_body(replace(MAIL, recipient_address="mallory@evil.example"))
    # Outside send_approval_request no request is being sent, so nothing may go out.
    with pytest.raises(GraphCallRefused, match="no stored approval request"):
        graph._post(f"users/{MAILBOX}/sendMail", approval_body(MAIL))
    graph._transport.expected_approval = MAIL
    with pytest.raises(GraphCallRefused, match="not the person on the request"):
        graph._post(f"users/{MAILBOX}/sendMail", other)
    assert reached == []


def test_another_client_cannot_send_an_approval_request() -> None:
    graph = GraphClient(StaticTokens(), MAILBOX, BASE_URL, forward_to="reminders@maxasp.example")
    with pytest.raises(GraphCallRefused):
        graph.send_approval_request(MAIL)


def test_the_client_checks_against_the_stored_request_not_what_it_is_handed() -> None:
    standin = create_app(token=TOKEN)

    def client(lookup: object) -> GraphClient:
        return GraphClient(
            StaticTokens(),
            MAILBOX,
            BASE_URL,
            transport=standin_transport(standin),
            calls=APPROVALS,
            approval_lookup=lookup,  # type: ignore[arg-type]
        )

    # No stored, unsent request with that ID: nothing goes out.
    with pytest.raises(GraphCallRefused, match="no unsent stored request"):
        client(lambda _: None).send_approval_request(MAIL)
    # The caller changes the address, but the database holds Alice: refused.
    with pytest.raises(GraphCallRefused, match="not the person on the request"):
        client(lambda _: MAIL).send_approval_request(
            replace(MAIL, recipient_address="mallory@evil.example")
        )
    # Without a lookup the client cannot send at all.
    with pytest.raises(GraphCallRefused, match="not the approval client"):
        client(None).send_approval_request(MAIL)
    assert standin.state.mailbox.notices == []
