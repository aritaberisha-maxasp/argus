"""The invoice status page without a database (#62, NF-05, NF-03).

Who may get the list, what the public page holds and that the page shows server text only as
text. The tests with real cases are in tests/integration/test_invoices_page.py.
"""

import re
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus.app import PUBLIC_PATHS, create_app
from argus.auth import Role
from argus.cases.model import CaseKind, CaseStatus, ClosedReason, ExceptionReason, Outcome
from argus.invoices_page.resolve import BadResolution, choices_for, clean_comment
from argus.invoices_page.views import STATUS_TEXT, _like
from argus.invoices_page.web import DATA_PATH, PAGE_PATHS, STATIC, CaseQuery
from standins.entra.app import FakeEntra

pytestmark = pytest.mark.req("NF-05", "NF-03")

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up

QUEUE_STATIC = files("argus.queue_page") / "static"


def no_database() -> Engine:
    raise RuntimeError("these tests have no database")


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(entra: FakeEntra) -> TestClient:
    return TestClient(create_app(no_database, sign_in=entra.verifier))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def script() -> str:
    return (STATIC / "invoices.js").read_text(encoding="utf-8")


# Who gets the list


def test_an_approver_gets_no_list_of_all_cases(entra: FakeEntra, client: TestClient) -> None:
    response = client.post(DATA_PATH, headers=bearer(entra.token(ALICE, roles=[Role.APPROVER])))

    assert response.status_code == 403
    assert response.json() == {"detail": "your role may not do this"}


def test_without_a_role_or_a_token_there_is_no_list(entra: FakeEntra, client: TestClient) -> None:
    assert client.post(DATA_PATH).status_code == 401
    assert client.post(DATA_PATH, headers=bearer(entra.token(ALICE, roles=[]))).status_code == 403


def test_the_list_is_not_reachable_with_get(entra: FakeEntra, client: TestClient) -> None:
    # A GET would carry the filters in the address, and addresses end up in logs (NF-03).
    finance = bearer(entra.token(BENJAMIN, roles=[Role.FINANCE]))

    assert client.get(DATA_PATH, headers=finance).status_code in (404, 405, 422)
    assert client.get("/cases", headers=finance).status_code in (404, 405)


# The public page


def test_the_page_is_the_same_fixed_file_for_everyone_and_needs_no_database(
    entra: FakeEntra, client: TestClient
) -> None:
    for path in PAGE_PATHS:
        anonymous = client.get(path)
        signed_in = client.get(path, headers=bearer(entra.token(BENJAMIN, roles=[Role.FINANCE])))
        assert anonymous.status_code == 200, path
        assert anonymous.content == signed_in.content, path
    assert PAGE_PATHS <= PUBLIC_PATHS
    assert DATA_PATH not in PUBLIC_PATHS


def test_the_page_runs_only_its_own_scripts_and_the_queue_page_look(client: TestClient) -> None:
    response = client.get("/invoices")
    policy = response.headers["content-security-policy"]

    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy
    assert response.headers["cache-control"] == "no-store"
    assert "<script>" not in response.text
    for shared in ("/queue/app.css", "/queue/msal-browser.min.js", "/queue/favicon.svg"):
        assert f'"{shared}"' in response.text, shared


def test_the_page_carries_the_sign_in_settings_of_the_local_fake(client: TestClient) -> None:
    page = client.get("/invoices").text

    assert '<meta name="argus-sign-in-mode" content="token">' in page
    assert "<!-- sign in settings -->" not in page


def test_the_page_needs_no_inline_style() -> None:
    page = (STATIC / "invoices.html").read_text(encoding="utf-8")

    assert "style=" not in page
    assert "<style" not in page
    assert ".style" not in script()
    assert "cssText" not in script()


def test_the_page_never_puts_server_text_into_html() -> None:
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in script(), unsafe


def test_the_search_word_stays_out_of_every_address() -> None:
    # The filters go in a POST body; the page address keeps status, kind and dates only.
    code = script()
    in_address = re.search(r"const IN_ADDRESS = \[([^\]]*)\]", code)

    assert in_address is not None
    assert '"q"' not in in_address.group(1)
    assert 'fetch("/cases/search", {\n      method: "POST"' in code


def test_the_static_files_ship_with_the_package() -> None:
    names = {p.name for p in Path(str(STATIC)).iterdir()}

    assert {"invoices.html", "invoices.js", "invoices.css"} <= names


# Getting there from the other pages


def test_the_queue_and_mailbox_pages_link_to_the_invoices() -> None:
    queue = (QUEUE_STATIC / "queue.html").read_text(encoding="utf-8")
    mailbox = (files("argus.mailbox_page") / "static" / "mailbox.html").read_text(encoding="utf-8")

    assert '<a href="/invoices">Invoices</a>' in queue
    assert '<a href="/invoices">Invoices</a>' in mailbox


def test_after_sign_in_the_queue_page_sends_people_back_only_to_fixed_pages() -> None:
    # The invoice page signs in through /queue (the one address in Entra) and comes back.
    code = (QUEUE_STATIC / "app.js").read_text(encoding="utf-8")
    found = re.search(r'back === "/mailbox" \|\| /(.+)/\.test\(back\)', code)

    assert found is not None
    allowed = re.compile(found.group(1).replace("\\/", "/"))
    for good in ("/invoices", "/invoices?status=booked", "/invoices?kind=bank_transaction&x=1"):
        assert allowed.fullmatch(good), good
    for bad in (
        "//evil.example",
        "/invoices//evil.example",
        "https://evil.example/invoices",
        "/invoices?<script>",
        "/invoicesX",
        "/queue",
    ):
        assert not allowed.fullmatch(bad), bad


# Words and filters


def test_every_status_has_plain_words() -> None:
    assert set(STATUS_TEXT) == set(CaseStatus)
    assert all(text and text[0].isupper() for text in STATUS_TEXT.values())


def test_a_search_word_matches_literally() -> None:
    assert _like("50%_off\\") == "%50\\%\\_off\\\\%"


def test_an_unknown_filter_is_refused() -> None:
    with pytest.raises(ValueError):
        CaseQuery.model_validate({"vendor": "x"})
    with pytest.raises(ValueError):
        CaseQuery.model_validate({"q": "x" * 101})
    assert CaseQuery.model_validate({}) == CaseQuery()


# Resolving an exception from the page (EX-01)


@pytest.mark.req("EX-01")
def test_an_approver_or_someone_without_a_token_cannot_resolve(
    entra: FakeEntra, client: TestClient
) -> None:
    approver = bearer(entra.token(ALICE, roles=[Role.APPROVER]))
    body = {"outcome": "rejected", "comment": "not ours"}

    assert client.get("/exceptions/1/choices", headers=approver).status_code == 403
    assert client.post("/exceptions/1/resolution", json=body, headers=approver).status_code == 403
    assert client.get("/exceptions/1/choices").status_code == 401
    assert client.post("/exceptions/1/resolution", json=body).status_code == 401


@pytest.mark.req("EX-01")
def test_an_unknown_outcome_or_extra_field_is_refused_before_the_database(
    entra: FakeEntra, client: TestClient
) -> None:
    finance = bearer(entra.token(BENJAMIN, roles=[Role.FINANCE]))
    path = "/exceptions/1/resolution"

    for body in (
        {"outcome": "paid", "comment": "not ours"},
        {"outcome": "rejected"},
        {"outcome": "rejected", "comment": "not ours", "case_to": "closed"},
    ):
        assert client.post(path, json=body, headers=finance).status_code == 422


@pytest.mark.req("EX-01")
@pytest.mark.parametrize("reason", list(ExceptionReason))
def test_every_reason_has_safe_endings(reason: ExceptionReason) -> None:
    for kind in CaseKind:
        for resume in [*CaseStatus, None]:
            for others in (0, 1):
                offered = choices_for(reason, kind, resume, others)
                outcomes = [c.outcome for c in offered]
                assert len(set(outcomes)) == len(outcomes)
                closing = [c for c in offered if c.closes]
                if closing:
                    # Only an incoming invoice or credit note that has not been booked or paid
                    # closes, and only when nothing else is open on it.
                    assert kind in (CaseKind.INCOMING_INVOICE, CaseKind.INCOMING_CREDIT_NOTE)
                    assert resume not in (
                        CaseStatus.BOOKED,
                        CaseStatus.IN_PAYMENT_RUN,
                        CaseStatus.PAID,
                    )
                    assert others == 0
                # The database rules of migrations 0002, 0010 and 0013 for these reasons.
                if reason is ExceptionReason.IBAN_MISMATCH:
                    assert outcomes in ([], [Outcome.REJECTED])
                if reason is ExceptionReason.VALUES_TO_CHECK:
                    assert outcomes in ([], [Outcome.REJECTED])
                if reason is ExceptionReason.DUPLICATE:
                    assert set(outcomes) <= {
                        Outcome.CONFIRMED_DUPLICATE,
                        Outcome.NOT_A_DUPLICATE,
                        Outcome.REJECTED,
                    }
                if reason is ExceptionReason.PAYMENT_FAILED:
                    assert outcomes == []
                assert Outcome.DATA_CORRECTED not in outcomes  # only through a correction


@pytest.mark.req("EX-01")
def test_a_plain_exception_on_a_new_invoice_can_carry_on_or_be_rejected() -> None:
    offered = choices_for(
        ExceptionReason.UNKNOWN_VENDOR, CaseKind.INCOMING_INVOICE, CaseStatus.RECEIVED, 0
    )

    assert [c.outcome for c in offered] == [Outcome.ACCEPTED_WITH_COMMENT, Outcome.REJECTED]
    assert offered[-1].closes is ClosedReason.REJECTED


@pytest.mark.req("EX-01", "NF-03")
@pytest.mark.parametrize("comment", ["", "   ", "ab", "x" * 501])
def test_a_comment_is_needed_and_short(comment: str) -> None:
    with pytest.raises(BadResolution, match="Leave out personal details"):
        clean_comment(comment)


def test_a_comment_is_kept_without_surrounding_spaces() -> None:
    assert clean_comment("  Checked with the vendor  ") == "Checked with the vendor"


def test_the_resolve_panel_puts_server_text_on_the_page_only_as_text() -> None:
    page = script()
    start = page.index("function resolveForm")
    part = page[start : page.index("// The fake mailbox page", start)]

    assert "innerHTML" not in part and "insertAdjacentHTML" not in part
    assert "x.detail" in part and "textContent" in page
