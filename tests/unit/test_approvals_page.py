"""The approval page without a database (#38): its routes, the comment rules and the way back
after sign in. The database rules are tested in tests/integration/test_approval_page.py."""

import re
from importlib.resources import files

import pytest

from argus.app import PUBLIC_PATHS
from argus.approvals_page.decide import BadComment, Choice, checked_comment
from argus.approvals_page.web import DATA_PATH, PAGE_PATHS

pytestmark = pytest.mark.req("AP-02", "AP-03", "AP-07", "NF-03")

QUEUE_STATIC = files("argus.queue_page") / "static"
STATIC = files("argus.approvals_page") / "static"


def test_the_page_is_public_and_the_data_is_not() -> None:
    assert PAGE_PATHS <= PUBLIC_PATHS
    assert "/approvals/{case_id}" in PUBLIC_PATHS
    assert not any(path.startswith(DATA_PATH) for path in PUBLIC_PATHS)


def test_after_sign_in_the_queue_page_sends_people_back_to_the_approval_page() -> None:
    """The approval email links to /approvals/<case ID>. Sign in goes through /queue, the one
    address in Entra, which sends the person back only to fixed addresses."""
    code = (QUEUE_STATIC / "app.js").read_text(encoding="utf-8")
    found = re.search(r"(/\^\\/approvals.+?\$/)\.test\(back\)", code)

    assert found is not None
    allowed = re.compile(found.group(1).strip("/").replace("\\/", "/"))
    for good in ("/approvals", "/approvals/1", "/approvals/9223372036854775807"):
        assert allowed.fullmatch(good), good
    for bad in (
        "//evil.example",
        "/approvals//evil.example",
        "/approvals/1/x",
        "/approvals/abc",
        "/approvals?next=https://evil.example",
        "https://evil.example/approvals",
    ):
        assert not allowed.fullmatch(bad), bad


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n", "​​​", "﻿ ok "])
def test_a_rejection_needs_a_comment_of_at_least_three_real_characters(blank: str | None) -> None:
    with pytest.raises(BadComment, match="at least 3 characters"):
        checked_comment(Choice.REJECT, blank)


def test_the_comment_is_trimmed_and_optional_for_an_approval() -> None:
    assert checked_comment(Choice.REJECT, "  Not ordered  ") == "Not ordered"
    assert checked_comment(Choice.APPROVE, None) is None
    assert checked_comment(Choice.APPROVE, " ​ ") is None
    assert checked_comment(Choice.APPROVE, "Fine") == "Fine"
    with pytest.raises(BadComment):
        checked_comment(Choice.APPROVE, "ok")
    with pytest.raises(BadComment, match="500"):
        checked_comment(Choice.REJECT, "x" * 501)


def test_the_page_puts_server_text_on_the_page_as_text_only() -> None:
    """Nothing read from an invoice may run on the page (NF-03)."""
    script = (STATIC / "approvals.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script


def test_a_blank_tax_key_is_said_in_words_with_a_warning() -> None:
    """Compliance review of PR 190: never an empty field."""
    script = (STATIC / "approvals.js").read_text(encoding="utf-8")
    assert '"No tax key"' in script
    assert "No input VAT will be booked" in script
