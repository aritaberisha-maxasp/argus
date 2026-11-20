"""The exception queue page without a database (#126, EX-01, EX-02, NF-03).

Who may get the queue, what the public page holds, how the age is counted and how a stored
email is shown. The tests with real exceptions are in tests/integration/test_queue_page.py.
"""

import hashlib
from datetime import UTC, date, datetime
from email.message import EmailMessage
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus.app import BLANK_REASON, PUBLIC_PATHS, create_app
from argus.auth import Role, TokenVerifier
from argus.auth.tokens import EntraSettings
from argus.cases.model import ExceptionReason
from argus.queue_page.views import REASON_TEXT, read_email
from argus.queue_page.web import PAGE_PATHS, STATIC, _download_name, render_page
from argus.working_days import working_days_between
from standins.entra.app import API_CLIENT_ID, ROLE_GROUPS, TENANT_ID, FakeEntra

pytestmark = pytest.mark.req("EX-01", "EX-02", "NF-03")

BENJAMIN = "33333333-3333-4333-8333-333333333333"  # finance, made up
ALICE = "11111111-1111-4111-8111-111111111111"  # approver, made up

# The Microsoft sign in library is vendored, so the page loads nothing from other sites. Any
# change to the file must be on purpose: update the version in static/README.md and this hash.
MSAL_SHA256 = "2b580165064a6a4ec041ebd44832299172688a23a42c3b81d65314799dba04eb"

# The Geist fonts (SIL Open Font License) are kept here too, from the npm package geist 1.9.2
# (#152). Same rule: update static/README.md and these hashes together.
FONT_SHA256 = {
    "Geist-Regular.woff2": "9b2554b1bab7a16b7e592f24a2928776ee337c1038739f6d09be95e16afcd5aa",
    "Geist-Medium.woff2": "5f0af5c55a6853b550e5b70bac054fb70aca977a1d7a3195f5c6953b6b19d553",
    "Geist-SemiBold.woff2": "adde6a779fb07a4fd4b52bc93d61cdcc8e3a466e6216de8ba7a84ff93fe02068",
    "GeistMono-Regular.woff2": "9f1fb60a7c2b9320990c8703c347508e814e010a5958cc610be4b020d18bb0c8",
    "GeistMono-Medium.woff2": "247179b5a0a8baca5f46fd0e031bb7af475a5293ccbaeee21920ecab29806846",
}


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


DATA_PATHS = ["/exceptions", "/exceptions/1", "/exceptions/1/files/1"]


# Who gets the queue


@pytest.mark.parametrize("path", DATA_PATHS)
def test_an_approver_gets_no_access_to_the_queue(
    entra: FakeEntra, client: TestClient, path: str
) -> None:
    response = client.get(path, headers=bearer(entra.token(ALICE, roles=[Role.APPROVER])))

    assert response.status_code == 403
    assert response.json() == {"detail": "your role may not do this"}


@pytest.mark.parametrize("path", DATA_PATHS)
def test_without_a_role_or_a_token_there_is_no_queue(
    entra: FakeEntra, client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers=bearer(entra.token(ALICE, roles=[]))).status_code == 403


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


def test_the_page_runs_only_its_own_scripts(client: TestClient) -> None:
    response = client.get("/queue")
    policy = response.headers["content-security-policy"]

    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy
    assert response.headers["cache-control"] == "no-store"
    assert "<script>" not in response.text  # no inline script, only the two files


def test_the_page_loads_its_fonts_and_icons_only_from_here(client: TestClient) -> None:
    page = client.get("/queue")
    policy = page.headers["content-security-policy"]

    assert "font-src 'self';" in policy
    assert "fonts.googleapis.com" not in page.text
    assert 'href="/queue/favicon.svg"' in page.text
    assert 'href="/queue/favicon-16.svg"' in page.text
    for name, media_type in [
        ("favicon.svg", "image/svg+xml"),
        ("favicon-16.svg", "image/svg+xml"),
        ("Geist-Regular.woff2", "font/woff2"),
        ("geist.LICENSE", "text/plain; charset=utf-8"),
    ]:
        response = client.get(f"/queue/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == media_type, name


def test_the_fonts_are_the_pinned_ones_with_their_license() -> None:
    for name, digest in FONT_SHA256.items():
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest, name
    license_text = (STATIC / "geist.LICENSE").read_text(encoding="utf-8")
    assert "SIL Open Font License" in license_text
    style = (STATIC / "app.css").read_text(encoding="utf-8")
    for name in FONT_SHA256:
        assert f'url("/queue/{name}")' in style, name


def test_the_page_needs_no_inline_style() -> None:
    page = (STATIC / "queue.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "style=" not in page
    assert "<style" not in page
    assert ".style" not in script
    assert "cssText" not in script
    # The demo mailbox page (#135) shares the look and the same CSP.
    mailbox = files("argus.mailbox_page") / "static"
    assert "style=" not in (mailbox / "mailbox.html").read_text(encoding="utf-8")
    assert ".style" not in (mailbox / "mailbox.js").read_text(encoding="utf-8")


def test_the_page_never_puts_server_text_into_html() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in script


def test_the_vendored_sign_in_library_is_the_pinned_one() -> None:
    data = (STATIC / "msal-browser.min.js").read_bytes()

    assert hashlib.sha256(data).hexdigest() == MSAL_SHA256
    assert data.startswith(b"/*! @azure/msal-browser v4.30.0 ")


def test_the_page_signs_in_with_microsoft_in_azure() -> None:
    settings = EntraSettings(TENANT_ID, API_CLIENT_ID, ROLE_GROUPS)
    page = render_page(TokenVerifier(settings, keys=lambda token: None))

    assert '<meta name="argus-sign-in-mode" content="microsoft">' in page
    assert (
        '<meta name="argus-sign-in-authority" '
        f'content="https://login.microsoftonline.com/{TENANT_ID}">'
    ) in page
    assert f'content="api://{API_CLIENT_ID}/access_as_user"' in page


def test_the_page_asks_for_a_fake_token_only_with_the_local_fake(entra: FakeEntra) -> None:
    assert '<meta name="argus-sign-in-mode" content="token">' in render_page(entra.verifier())


def test_without_sign_in_settings_the_page_says_so() -> None:
    page = render_page(None)

    assert '<meta name="argus-sign-in-mode" content="off">' in page
    assert "client-id" not in page


# What the page shows


def test_every_reason_has_plain_words() -> None:
    assert set(REASON_TEXT) == set(ExceptionReason)


def berlin(day: str, hour: int = 10) -> datetime:
    # 09:00 UTC is 10:00 or 11:00 in Berlin, the same calendar day either way.
    return datetime.fromisoformat(f"{day}T{hour - 1:02d}:00:00+00:00").astimezone(UTC)


@pytest.mark.parametrize(
    ("opened", "now", "age"),
    [
        ("2026-11-10", "2026-11-10", 0),  # opened today
        ("2026-11-10", "2026-11-11", 1),  # Tuesday to Wednesday
        ("2026-11-13", "2026-11-16", 1),  # Friday to Monday
        ("2026-11-14", "2026-11-16", 1),  # opened on a Saturday, Monday is the first day
        ("2026-12-24", "2026-12-28", 1),  # Christmas Day (a Friday) is a holiday
        ("2026-11-02", "2026-11-16", 10),  # two full weeks
        ("2026-11-16", "2026-11-10", 0),  # a clock behind never gives a negative age
    ],
)
def test_the_age_counts_working_days_in_baden_wuerttemberg(opened: str, now: str, age: int) -> None:
    assert working_days_between(berlin(opened), berlin(now)) == age


def test_late_friday_evening_utc_is_already_saturday_in_berlin() -> None:
    friday_night = datetime(2026, 11, 13, 23, 30, tzinfo=UTC)  # 00:30 Saturday in Berlin

    assert working_days_between(friday_night, date(2026, 11, 16)) == 1


def test_a_stored_email_is_read_as_text() -> None:
    message = EmailMessage()
    message["From"] = "Rechnung <rechnung@lieferant.example>"
    message["To"] = "buchhaltung@max-asp.example"
    message["Subject"] = "Rechnung 4711"
    message["Date"] = "Tue, 10 Nov 2026 09:00:00 +0100"
    message.set_content("<b>Hallo</b>, anbei die Rechnung.")

    view = read_email(message.as_bytes())

    assert view.sender == "Rechnung <rechnung@lieferant.example>"
    assert view.subject == "Rechnung 4711"
    assert view.cc == ""
    assert view.body.strip() == "<b>Hallo</b>, anbei die Rechnung."  # shown as text, as is


def test_a_broken_email_still_opens() -> None:
    view = read_email(b"\xff\xfe not an email at all")

    assert view.sender == ""


def test_a_download_name_cannot_break_the_response() -> None:
    header = _download_name(7, "attachment", 'Rechnung "März"\r\nX-Evil: 1.pdf')

    assert "\r" not in header and "\n" not in header
    assert header.startswith('attachment; filename="argus-7"; ')
    assert _download_name(3, "email", None).startswith('attachment; filename="argus-3.eml"')


def test_the_static_files_ship_with_the_package() -> None:
    names = {p.name for p in Path(str(STATIC)).iterdir()}

    assert {
        "queue.html",
        "app.js",
        "app.css",
        "msal-browser.min.js",
        "favicon.svg",
        "favicon-16.svg",
        "geist.LICENSE",
        *FONT_SHA256,
    } <= names


# Shadow or live mode on the page (#134)


@pytest.mark.req("IN-01", "NF-01")
def test_the_page_switches_the_mode_only_through_the_existing_route() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert '"/intake/mode"' in script
    assert 'call(token, "/intake/mode", "PUT", { mode: target, reason: why })' in script
    # The switch is shown only when ARGUS says this person may switch (the admin).
    assert "if (data.may_switch)" in script
    # No other route that writes. The only other writes are the correction of a case (#112)
    # and the G/L account a person enters (#37), each one POST from send(), and send() is used
    # for those two routes only. Every other call is a GET.
    assert script.count('"PUT"') == 1
    assert script.count('"POST"') == 1
    assert script.count("await send(") == 2
    assert '/correction"' in script
    assert '"/account"' in script
    for other in ('"PATCH"', '"DELETE"'):
        assert other not in script


@pytest.mark.req("NF-03")
@pytest.mark.parametrize("role", [Role.APPROVER, None])
def test_an_approver_or_a_person_without_a_role_sees_no_mode(
    entra: FakeEntra, client: TestClient, role: Role | None
) -> None:
    token = entra.token(ALICE, roles=[role] if role else [])
    assert client.get("/intake/mode", headers=bearer(token)).status_code == 403
    assert client.get("/intake/mode").status_code == 401


@pytest.mark.req("NF-01", "NF-03")
def test_the_mode_form_treats_tabs_and_line_breaks_as_a_blank_reason() -> None:
    """#170: the page trims like the server and the database, and shows a sentence for a
    refused reason instead of a general error."""
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "const why = trimReason(reason.value);" in script
    assert r"/^[\s\u180e\u200b\ufeff]+|[\s\u180e\u200b\ufeff]+$/g" in script
    assert BLANK_REASON in script
    assert "problem.textContent = e.detail || BLANK_REASON;" in script
