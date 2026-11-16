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

Logs name only the person's ID and counts, never a vendor, customer or search word (NF-03).
"""

import html
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from importlib.resources import files
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from argus.auth import Role, SignedInUser, require_role
from argus.auth.tokens import TokenVerifier
from argus.cases.model import CaseKind, CaseStatus
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


class CaseQuery(BaseModel):
    """The filters of the list. Every part is optional; an unknown part is refused."""

    model_config = ConfigDict(extra="forbid")

    status: CaseStatus | None = None
    kind: CaseKind | None = None
    q: Annotated[str | None, Field(max_length=MAX_SEARCH)] = None
    received_from: date | None = None
    received_to: date | None = None


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
        return Response(
            json.dumps(list_json(found), default=str),
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
