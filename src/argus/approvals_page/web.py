"""The approval page, after Microsoft sign in (#38, AP-02, AP-03, AP-04, AP-07, AP-08, PR-04).

The approval request email (#191) links to ``/approvals/<case ID>``. Like the other pages there
are two kinds of routes:

- **The page** (``/approvals``, ``/approvals/<case ID>`` and its script and style). A fixed file
  without business data, open like the health checks. It uses the queue page's look, fonts and
  Microsoft sign in library, and signs in through ``/queue``, the one address registered in
  Entra.
- **The data** (``/approval-requests/...``), for anyone signed in with an ARGUS role:
  - An approver sees only the invoices whose newest approval request went to them, as approver
    or deputy. Another approver's invoice answers 403, and so does one that does not exist, so
    an approver cannot probe for case numbers.
  - Finance and admin see every waiting invoice, read only.
  - Only the person the newest request went to decides or changes the account. Anyone else,
    finance and admin too, gets 403 (ADR 0010). The database checks it again (migration 0021).
  - The invoice file is served only for a case the person may see, and only the case's own
    invoice file. A PDF or image is shown on the page in a sandbox; anything else downloads.

No route reaches DATEV or Wise, so the page works with both switched off (AP-04). Nothing here
books or pays (NF-04). Logs name only the person's ID, case, request and decision IDs and the
outcome, never a vendor, name, comment or reason (NF-03).
"""

import html
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from importlib.resources import files
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from argus.approvals_page.decide import (
    COMMENT_MAX,
    BadComment,
    Choice,
    NotWaiting,
    ProposalChanged,
    VendorChanged,
    change_account,
    decide,
)
from argus.approvals_page.views import (
    ApprovalView,
    approval_view,
    file_of_case,
    is_asked,
    newest_request,
    waiting_rows,
)
from argus.auth import CurrentUser, SignedInUser
from argus.auth.roles import SEES_EVERY_CASE
from argus.auth.tokens import TokenVerifier
from argus.cases.store import CaseRuleBroken, unit_of_work
from argus.documents.store import DocumentStore
from argus.intake.accounts import BadAccount, Chart, EnteredBy, check_entered
from argus.queue_page.web import _sign_in_mode

log = logging.getLogger(__name__)

STATIC = files("argus.approvals_page") / "static"

PAGE_PATH = "/approvals"
CASE_PAGE_PATH = "/approvals/{case_id}"
_ASSETS = {
    "/approvals/app.js": ("approvals.js", "text/javascript; charset=utf-8"),
    "/approvals/app.css": ("approvals.css", "text/css; charset=utf-8"),
}
# The route templates of the page. The case page is the same fixed file for every case number.
PAGE_PATHS = frozenset({PAGE_PATH, CASE_PAGE_PATH} | set(_ASSETS))

DATA_PATH = "/approval-requests"
CaseId = Annotated[int, Path(ge=1, le=2**63 - 1)]
NOT_YOURS = "this invoice does not wait for your decision"
PROPOSAL_CHANGED = (
    "The G/L account or tax key changed since you opened the invoice. Check it, then decide again."
)
VENDOR_CHANGED = (
    "This invoice now belongs to another vendor, so that vendor's approver has to decide. "
    "Nothing was saved. Ask Benjamin to send it to the right approver."
)


class DecisionForm(BaseModel):
    """What the approver sends. Checked again by the database."""

    model_config = ConfigDict(extra="forbid")

    request_id: int = Field(ge=1, le=2**63 - 1)  # the request the page showed
    # The account proposal the page showed. Required to approve (AP-08).
    proposal_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    choice: Choice
    # Longer than the stored limit, so a long comment gets the plain sentence, not a 422 list.
    comment: Annotated[str | None, Field(max_length=COMMENT_MAX * 4)] = None


class AccountForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: int = Field(ge=1, le=2**63 - 1)
    gl_account: Annotated[str, Field(max_length=40)]
    tax_key: Annotated[str, Field(max_length=40)]
    reason: Annotated[str, Field(max_length=2000)]


def _page_headers() -> dict[str, str]:
    # As the queue page, plus blob: so the page can show the invoice file it fetched with the
    # person's token (an <img> or a PDF frame).
    login = "https://login.microsoftonline.com"
    return {
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; font-src 'self'; "
            f"connect-src 'self' {login}; frame-src {login} blob:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def render_page(verifier: TokenVerifier | None) -> str:
    meta = "\n".join(
        f'  <meta name="argus-sign-in-{html.escape(k)}" content="{html.escape(v)}">'
        for k, v in _sign_in_mode(verifier).items()
    )
    template = (STATIC / "approvals.html").read_text(encoding="utf-8")
    return template.replace("  <!-- sign in settings -->", meta)


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _json(body: dict[str, Any], status_code: int = status.HTTP_200_OK) -> Response:
    return Response(
        json.dumps(body, default=_plain),
        status_code=status_code,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def view_json(view: ApprovalView, user: SignedInUser) -> dict[str, Any]:
    out = asdict(view)
    asked = is_asked(view.request, user.object_id)
    out["waiting"] = view.waiting
    out["may_decide"] = asked and view.waiting
    out["you_were_asked"] = asked
    # Object IDs are not for the page; the name says who.
    out["request"].pop("recipient_id")
    if out["decision"] is not None:
        out["decision"].pop("decided_by")
    return out


def _sees_all(user: SignedInUser) -> bool:
    return bool(user.roles & SEES_EVERY_CASE)


def install_approvals_page(
    app: FastAPI,
    engine: Callable[[], Engine],
    documents: Callable[[], DocumentStore],
    chart: Chart = Chart.SKR03,
) -> None:
    """Add the page and its data routes."""

    def page(request: Request) -> HTMLResponse:
        verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
        return HTMLResponse(render_page(verifier), headers=_page_headers())

    # The script and style first, so /approvals/app.js never reads as a case number.
    for path, (name, media_type) in _ASSETS.items():

        def asset(name: str = name, media_type: str = media_type) -> Response:
            body = (STATIC / name).read_bytes()
            return Response(body, media_type=media_type, headers=_page_headers())

        app.add_api_route(path, asset, methods=["GET"], include_in_schema=False)

    app.add_api_route(PAGE_PATH, page, methods=["GET"], include_in_schema=False)

    def case_page(request: Request, case_id: CaseId) -> HTMLResponse:
        return page(request)

    app.add_api_route(CASE_PAGE_PATH, case_page, methods=["GET"], include_in_schema=False)

    def readable(user: SignedInUser, case_id: int) -> ApprovalView:
        """The case's approval page if this person may see it, else 403 (or 404 for finance
        and admin, who may see any case)."""
        with engine().connect() as connection:
            found = approval_view(connection, case_id)
        if found is not None and (is_asked(found.request, user.object_id) or _sees_all(user)):
            return found
        if found is None and _sees_all(user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no approval request for this case")
        raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)

    def only_the_one_asked(user: SignedInUser, case_id: int) -> None:
        with engine().connect() as connection:
            request = newest_request(connection, case_id)
        if not is_asked(request, user.object_id):
            log.info("user %s may not decide on case %s", user.object_id, case_id)
            raise HTTPException(status.HTTP_403_FORBIDDEN, NOT_YOURS)

    def stands_now(user: SignedInUser, case_id: int) -> Response:
        """What was decided, by whom and when, for a second click, a second tab or an old link."""
        found = readable(user, case_id)
        return _json(
            {
                "problem": "This invoice no longer waits for this decision. See what stands now.",
                "approval": view_json(found, user),
            },
            status.HTTP_409_CONFLICT,
        )

    def changed_now(user: SignedInUser, case_id: int, problem: str) -> Response:
        """The invoice still waits, but not as the page showed it. The page shows it again."""
        log.info("user %s: case %s changed since the page showed it", user.object_id, case_id)
        return _json(
            {"problem": problem, "approval": view_json(readable(user, case_id), user)},
            status.HTTP_409_CONFLICT,
        )

    @app.get(DATA_PATH)
    def list_waiting(user: CurrentUser) -> Response:
        """The invoices that wait for a decision: an approver's own, or all of them for finance
        and admin (read only)."""
        with engine().connect() as connection:
            rows = waiting_rows(connection, None if _sees_all(user) else user.object_id)
        log.info("user %s listed invoices waiting for approval (%d)", user.object_id, len(rows))
        out = []
        for row in rows:
            item = asdict(row)
            item["may_decide"] = row.recipient_id.lower() == user.object_id.lower()
            item.pop("recipient_id")
            out.append(item)
        return _json({"approvals": out, "sees_all": _sees_all(user)})

    @app.get(DATA_PATH + "/{case_id}")
    def read_one(case_id: CaseId, user: CurrentUser) -> Response:
        """One invoice: its data, image, the account proposal and, once made, the decision."""
        found = readable(user, case_id)
        log.info("user %s opened the approval page of case %s", user.object_id, case_id)
        return _json(view_json(found, user))

    @app.get(DATA_PATH + "/{case_id}/files/{document_id}")
    def invoice_file(case_id: CaseId, document_id: CaseId, user: CurrentUser) -> Response:
        """The stored invoice file, exactly as it came (#20). A PDF or image may show on the
        page, in a sandbox that runs nothing; anything else is a download."""
        readable(user, case_id)
        with engine().connect() as connection:
            found = file_of_case(connection, case_id, document_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
        sha256, content_type = found
        try:
            data = documents().get(sha256)
        except Exception as problem:
            log.error(
                "case %s: invoice file %s could not be read (%s)",
                case_id,
                document_id,
                type(problem).__name__,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "the file could not be loaded right now"
            ) from None
        inline = content_type in ("application/pdf", "image/png", "image/jpeg")
        log.info("user %s loaded invoice file %s of case %s", user.object_id, document_id, case_id)
        return Response(
            data,
            media_type=content_type if inline else "application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": (
                    "inline" if inline else f'attachment; filename="argus-{document_id}"'
                ),
            },
        )

    @app.post(DATA_PATH + "/{case_id}/decision")
    def make_decision(case_id: CaseId, form: DecisionForm, user: CurrentUser) -> Response:
        """Approve, or reject with a comment (AP-03, AP-07). Only the person asked."""
        only_the_one_asked(user, case_id)
        try:
            with unit_of_work(engine(), user.actor) as db:
                done = decide(
                    db, case_id, form.request_id, form.choice, form.comment, form.proposal_id
                )
        except BadComment as problem:
            return _json({"problem": str(problem)}, status.HTTP_422_UNPROCESSABLE_ENTITY)
        except NotWaiting:
            return stands_now(user, case_id)
        except ProposalChanged:
            return changed_now(user, case_id, PROPOSAL_CHANGED)
        except VendorChanged:
            return changed_now(user, case_id, VENDOR_CHANGED)
        except CaseRuleBroken as refusal:
            log.warning(
                "user %s: decision on case %s refused (%s)",
                user.object_id,
                case_id,
                type(refusal).__name__,
            )
            # A second tab may have decided between the check and the insert.
            with engine().connect() as connection:
                found = approval_view(connection, case_id)
            if found is not None and not found.waiting:
                return stands_now(user, case_id)
            return _json(
                {"problem": f"ARGUS refused the decision: {refusal}."}, status.HTTP_409_CONFLICT
            )
        log.info(
            "user %s decided on case %s: %s (decision %s)",
            user.object_id,
            case_id,
            form.choice.value,
            done.decision_id,
        )
        return _json(view_json(readable(user, case_id), user))

    @app.post(DATA_PATH + "/{case_id}/account")
    def change_gl_account(case_id: CaseId, form: AccountForm, user: CurrentUser) -> Response:
        """Change the proposed G/L account or tax key before deciding, with a reason (Arita on
        #38, 23 Nov). The same rules as the queue form apply. Only the person asked."""
        only_the_one_asked(user, case_id)
        try:
            entered = check_entered(chart, form.gl_account, form.tax_key, form.reason)
        except BadAccount as problem:
            return _json({"problem": str(problem)}, status.HTTP_422_UNPROCESSABLE_ENTITY)
        by = EnteredBy(user.actor, user.name, user.sign_in_address)
        try:
            with unit_of_work(engine(), user.actor) as db:
                proposal_id = change_account(db, case_id, form.request_id, entered, by, chart)
        except NotWaiting:
            return stands_now(user, case_id)
        except VendorChanged:
            return changed_now(user, case_id, VENDOR_CHANGED)
        except CaseRuleBroken as refusal:
            log.warning(
                "user %s: account change on case %s refused (%s)",
                user.object_id,
                case_id,
                type(refusal).__name__,
            )
            return _json(
                {"problem": f"ARGUS refused the account: {refusal}."}, status.HTTP_409_CONFLICT
            )
        log.info(
            "user %s changed the account of case %s at approval (proposal %s)",
            user.object_id,
            case_id,
            proposal_id,
        )
        return _json(view_json(readable(user, case_id), user))
