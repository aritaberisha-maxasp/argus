"""The fake mailbox page, in demo mode only (#135, IN-04, IN-06, NF-03, ADR 0009).

The fake mailbox has no public address on purpose. This read only page lets the people running
the demo see where ARGUS sorted each email: the Inbox, the ARGUS folders and Removed. At go
live people look at the real mailbox in Outlook, so these routes exist only while demo mode is
on (``ARGUS_DEMO``). Without it the app has no such routes and they answer 404.

Like the exception queue page (#126), the page itself is a fixed file without data and open to
anyone. It signs the person in through the queue page and asks ``/demo/mailbox`` for the data,
which only finance and admin get. It reads through the Graph client built from the demo
settings, so it can only reach the fake Graph.

Logs name only counts and IDs, never a subject or sender (NF-03).
"""

import html
import json
import logging
from collections.abc import Callable
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from argus.auth import Role, SignedInUser, require_role
from argus.auth.tokens import TokenVerifier
from argus.intake.graph import GraphCall, GraphClient
from argus.mailbox_page.views import folders_json, mailbox_folders
from argus.queue_page.web import _page_headers, _sign_in_mode

log = logging.getLogger(__name__)

STATIC = files("argus.mailbox_page") / "static"

# The page and its own files. Open like /queue; it reuses the queue page's style and the
# Microsoft sign in library from there.
PAGE_PATHS = frozenset({"/mailbox", "/mailbox/app.js", "/mailbox/app.css"})
DATA_PATH = "/demo/mailbox"
_ASSETS = {
    "/mailbox/app.js": ("mailbox.js", "text/javascript; charset=utf-8"),
    "/mailbox/app.css": ("mailbox.css", "text/css; charset=utf-8"),
}

MailboxReader = Annotated[SignedInUser, Depends(require_role(Role.FINANCE, Role.ADMIN))]


def render_page(verifier: TokenVerifier | None) -> str:
    """The page with the same sign in settings as the queue page."""
    meta = "\n".join(
        f'  <meta name="argus-sign-in-{html.escape(k)}" content="{html.escape(v)}">'
        for k, v in _sign_in_mode(verifier).items()
    )
    template = (STATIC / "mailbox.html").read_text(encoding="utf-8")
    return template.replace("  <!-- sign in settings -->", meta)


# The only Graph calls the demo mailbox page makes: list the folders and the emails in one
# (views.py). It never moves, deletes or sends (#168).
MAILBOX_PAGE_CALLS = frozenset({GraphCall.FIND_FOLDERS, GraphCall.LIST_ANY_FOLDER})


def install_mailbox_page(app: FastAPI, graph: Callable[[], GraphClient]) -> None:
    """Add the page and its data route. Call this in demo mode only."""

    @app.get("/mailbox", include_in_schema=False)
    def mailbox_page(request: Request) -> HTMLResponse:
        verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
        return HTMLResponse(render_page(verifier), headers=_page_headers())

    for path, (name, media_type) in _ASSETS.items():

        def asset(name: str = name, media_type: str = media_type) -> Response:
            body = (STATIC / name).read_bytes()
            return Response(body, media_type=media_type, headers=_page_headers())

        app.add_api_route(path, asset, methods=["GET"], include_in_schema=False)

    @app.get(DATA_PATH)
    def read_mailbox(user: MailboxReader) -> Response:
        """Every folder of the fake mailbox with the subject, sender and received time of its
        emails. Read only: nothing is moved, marked read or changed."""
        try:
            with graph() as client:
                mailbox = client.mailbox
                folders = mailbox_folders(client)
        except Exception as problem:
            # The type only: a message could carry a folder or email detail (NF-03).
            log.error("the fake mailbox could not be read (%s)", type(problem).__name__)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "the fake mailbox could not be read right now"
            ) from None
        log.info(
            "user %s looked at the fake mailbox (%d folders, %d emails)",
            user.object_id,
            len(folders),
            sum(len(f.emails) for f in folders),
        )
        return Response(
            json.dumps(folders_json(mailbox, folders)),
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
