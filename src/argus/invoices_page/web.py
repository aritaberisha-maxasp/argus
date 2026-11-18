"""The invoice status page, after Microsoft sign in (#62, NF-05, NF-03, EX-01).

Benjamin and Arita see every case and where it stands: received, awaiting approval, approved,
booked, in payment run, paid, exception or closed.

Like the exception queue page (#126), there are two kinds of routes:

- **The page** (``/invoices`` and its script and style). A fixed file without business data, open
  like the health checks. It uses the queue page's look, fonts and Microsoft sign in library, and
  signs in through ``/queue``, the one address registered in Entra.
- **The data** (``POST /cases/search``, the filters in the body). Only the finance role
  (Benjamin) and the admin role (Arita) see every case. An approver gets 403 here: approvers
  will see their own cases once #34 and #38 say who approves what. Someone without a token
  gets 401.
- **Resolving an exception** (``GET /exceptions/{id}/choices`` and
  ``POST /exceptions/{id}/resolution``, EX-01). The same two roles see how an open exception of
  a case can end and pick one with a short comment. See ``argus.invoices_page.resolve`` for
  which endings are offered and which rules the database checks; it records who and when.
- **The trail of a case** (``GET /cases/{id}/trail`` and ``GET /cases/{id}/trail.csv``, NF-01,
  AP-08). The same two roles read every step of one case, from its email to its last status,
  and download it as a file. Both only read, in a read only transaction; there is no route that
  changes the trail. See ``argus.invoices_page.trail``.

Logs name only the person's ID, case and exception IDs, counts and outcome codes, never a
vendor, customer, search word or comment (NF-03).
"""

import html
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from importlib.resources import files
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from argus.auth import Role, SignedInUser, require_role
from argus.auth.tokens import TokenVerifier
from argus.cases.model import CaseKind, CaseStatus, Outcome
from argus.cases.store import CaseRuleBroken, unit_of_work, verify_chain
from argus.invoices_page.resolve import (
    MAX_COMMENT,
    AlreadyResolved,
    BadResolution,
    NotResolvable,
    OpenCaseException,
    open_case_exception,
    resolve_from_page,
)
from argus.invoices_page.trail import CaseTrail, berlin, case_trail, trail_file
from argus.invoices_page.views import (
    MAX_ROWS,
    MAX_SEARCH,
    STATUS_TEXT,
    CaseFilter,
    CaseList,
    CaseRow,
    case_rows,
)
from argus.queue_page.web import _page_headers, _sign_in_mode

log = logging.getLogger(__name__)

STATIC = files("argus.invoices_page") / "static"

PAGE_PATH = "/invoices"
# A POST with the filters in the body, so a search word (often a vendor's or a person's name)
# never appears in an address and so never in an access log (NF-03).
DATA_PATH = "/cases/search"
_ASSETS = {
    "/invoices/app.js": ("invoices.js", "text/javascript; charset=utf-8"),
    "/invoices/app.css": ("invoices.css", "text/css; charset=utf-8"),
}
PAGE_PATHS = frozenset({PAGE_PATH} | set(_ASSETS))

# Finance and admin see every case (NF-03). Approvers do not, see above.
CaseReader = Annotated[SignedInUser, Depends(require_role(Role.FINANCE, Role.ADMIN))]
# A case ID as the database stores it (bigint). Anything else is a 422, never a database error.
CaseId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class CaseQuery(BaseModel):
    """The filters of the list. Every part is optional; an unknown part is refused."""

    model_config = ConfigDict(extra="forbid")

    status: CaseStatus | None = None
    kind: CaseKind | None = None
    q: Annotated[str | None, Field(max_length=MAX_SEARCH)] = None
    received_from: date | None = None
    received_to: date | None = None


class ResolutionForm(BaseModel):
    """How the person ends the exception, and why. Checked again by the database."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    # Longer than the stored limit, so a long comment gets the plain sentence, not a 422 list.
    comment: Annotated[str, Field(max_length=MAX_COMMENT * 4)]


def render_page(verifier: TokenVerifier | None) -> str:
    """The page with the same sign in settings as the queue page."""
    meta = "\n".join(
        f'  <meta name="argus-sign-in-{html.escape(k)}" content="{html.escape(v)}">'
        for k, v in _sign_in_mode(verifier).items()
    )
    template = (STATIC / "invoices.html").read_text(encoding="utf-8")
    return template.replace("  <!-- sign in settings -->", meta)


def row_json(row: CaseRow) -> dict[str, Any]:
    out = asdict(row)
    out["received_at"] = row.received_at.isoformat()
    out["updated_at"] = row.updated_at.isoformat()
    return out


def list_json(found: CaseList) -> dict[str, Any]:
    return {
        "cases": [row_json(r) for r in found.rows],
        # Every status, also those with no case, so the page always shows the same tabs.
        "statuses": [
            {"status": s.value, "text": text, "count": found.counts.get(s, 0)}
            for s, text in STATUS_TEXT.items()
        ],
        "total": found.total,
        "truncated": found.truncated,
        "limit": MAX_ROWS,
    }


def choices_json(found: OpenCaseException) -> dict[str, Any]:
    return {
        "exception_id": found.exception_id,
        "case_id": found.case_id,
        "kind": found.kind.value,
        "reason": found.reason.value,
        "reason_text": found.reason_text,
        "detail": found.detail,
        "resume_status": found.resume_status.value if found.resume_status else None,
        "resume_text": STATUS_TEXT[found.resume_status] if found.resume_status else None,
        "others_open": found.others_open,
        "choices": [
            {
                "outcome": c.outcome.value,
                "label": c.label,
                "explain": c.explain,
                "closes": c.closes.value if c.closes else None,
            }
            for c in found.choices
        ],
        "note": found.note,
    }


def trail_json(trail: CaseTrail) -> dict[str, Any]:
    return {
        "case": row_json(trail.case),
        "steps": [
            {
                "chain_seq": s.chain_seq,
                "occurred_at": s.occurred_at.isoformat(),
                "event_type": s.event_type,
                "what": s.what,
                "actor_kind": s.actor_kind,
                "actor": s.actor,
                "actor_name": s.actor_name,
                "from_status": s.from_status.value if s.from_status else None,
                "to_status": s.to_status.value if s.to_status else None,
                "facts": s.facts,
                "comment": s.comment,
                "hash": s.hash_hex,
            }
            for s in trail.steps
        ],
        "truncated": trail.truncated,
    }


def _read_only(connection: Connection) -> None:
    """Makes this transaction read only, so the trail routes cannot change anything even by
    mistake: the database would refuse it."""
    connection.execute(text("set transaction read only"))


def _json(body: dict[str, Any], status_code: int = status.HTTP_200_OK) -> Response:
    return Response(
        json.dumps(body, default=str),
        status_code=status_code,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def install_invoices_page(app: FastAPI, engine: Callable[[], Engine]) -> None:
    """Add the page and its data route."""

    @app.get(PAGE_PATH, include_in_schema=False)
    def invoices_page(request: Request) -> HTMLResponse:
        verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
        return HTMLResponse(render_page(verifier), headers=_page_headers())

    for path, (name, media_type) in _ASSETS.items():

        def asset(name: str = name, media_type: str = media_type) -> Response:
            body = (STATIC / name).read_bytes()
            return Response(body, media_type=media_type, headers=_page_headers())

        app.add_api_route(path, asset, methods=["GET"], include_in_schema=False)

    @app.post(DATA_PATH)
    def list_cases(user: CaseReader, wanted: CaseQuery | None = None) -> Response:
        """Every case with its status, vendor or customer, amount and dates, newest first
        (NF-05). The filters are optional and combine."""
        wanted = wanted or CaseQuery()
        filters = CaseFilter(
            status=wanted.status,
            kind=wanted.kind,
            search=wanted.q,
            received_from=wanted.received_from,
            received_to=wanted.received_to,
        )
        with engine().connect() as connection:
            found = case_rows(connection, filters)
        log.info(
            "user %s listed cases (%d shown of %d, filtered: %s)",
            user.object_id,
            len(found.rows),
            found.total,
            "yes" if filters != CaseFilter() else "no",
        )
        return _json(list_json(found))

    @app.get("/exceptions/{exception_id}/choices")
    def exception_choices(exception_id: int, user: CaseReader) -> Response:
        """How this open exception of a case can end from the page (EX-01)."""
        try:
            with engine().connect() as connection:
                found = open_case_exception(connection, exception_id)
        except NotResolvable:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no open exception of a case") from None
        log.info(
            "user %s looked at how exception %s of case %s can end (%d choices)",
            user.object_id,
            exception_id,
            found.case_id,
            len(found.choices),
        )
        return _json(choices_json(found))

    @app.post("/exceptions/{exception_id}/resolution")
    def resolve(exception_id: int, form: ResolutionForm, user: CaseReader) -> Response:
        """Resolve one open exception of a case as the person chose, with their comment.
        The case carries on or closes in the same transaction (EX-01)."""
        try:
            with unit_of_work(engine(), user.actor) as db:
                done = resolve_from_page(db, exception_id, form.outcome, form.comment)
        except BadResolution as problem:
            return _json({"problem": str(problem)}, status.HTTP_422_UNPROCESSABLE_ENTITY)
        except NotResolvable:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no open exception of a case") from None
        except AlreadyResolved:
            return _json(
                {"problem": "Someone resolved this exception a moment ago. Reload the list."},
                status.HTTP_409_CONFLICT,
            )
        except CaseRuleBroken as refusal:
            # The database's sentence names IDs only, never the comment.
            log.warning(
                "user %s: resolving exception %s as %s refused (%s)",
                user.object_id,
                exception_id,
                form.outcome.value,
                type(refusal).__name__,
            )
            return _json({"problem": f"ARGUS refused this: {refusal}."}, status.HTTP_409_CONFLICT)
        log.info(
            "user %s resolved exception %s of case %s as %s (case now %s)",
            user.object_id,
            exception_id,
            done.case_id,
            done.outcome.value,
            done.case_status.value,
        )
        return _json(
            {
                "case_id": done.case_id,
                "outcome": done.outcome.value,
                "case_status": done.case_status.value,
                "case_status_text": STATUS_TEXT[done.case_status],
                "others_open": done.others_open,
            }
        )

    @app.get("/cases/{case_id}/trail")
    def trail(case_id: CaseId, user: CaseReader) -> Response:
        """Every step of one case, oldest first, with time, actor and what happened (NF-01)."""
        with engine().connect() as connection:
            _read_only(connection)
            found = case_trail(connection, case_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such case")
        log.info(
            "user %s read the trail of case %s (%d steps)",
            user.object_id,
            case_id,
            len(found.steps),
        )
        return _json(trail_json(found))

    @app.get("/cases/{case_id}/trail.csv")
    def trail_download(case_id: CaseId, user: CaseReader) -> Response:
        """The trail of one case as a file for a spreadsheet, with a line that says whether the
        whole hash chain of the audit log was intact when the file was made (NF-01)."""
        with engine().connect() as connection:
            _read_only(connection)
            found = case_trail(connection, case_id)
            broken_at = verify_chain(connection) if found is not None else None
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such case")
        now = datetime.now(UTC)
        who = f"{user.name} ({user.object_id})" if user.name else user.object_id
        body = trail_file(found, chain_broken_at=broken_at, exported_at=now, exported_by=who)
        log.info(
            "user %s exported the trail of case %s (%d steps, chain %s)",
            user.object_id,
            case_id,
            len(found.steps),
            "intact" if broken_at is None else f"broken at {broken_at}",
        )
        day = berlin(now)[:10]
        return Response(
            # A byte order mark, so a spreadsheet opens umlauts correctly.
            "\ufeff" + body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    f'attachment; filename="argus-case-{case_id}-trail-{day}.csv"'
                ),
            },
        )
