"""The exception queue for Benjamin, after Microsoft sign in (#126, EX-01, EX-02, NF-03).

Two kinds of routes:

- **The page** (``/queue`` and its script, style and the Microsoft sign in library). It is the
  same fixed file for everyone and holds no business data, so it is public like the health
  checks. In the browser it signs the person in with Microsoft (ADR 0008) and then asks the
  routes below for the data, sending the access token.
- **The data** (``/exceptions/...``). Only a signed in person with the finance role (Benjamin)
  or the admin role (Arita) gets it. An approver gets 403, someone without a token 401.

Logs name only IDs, never a sender, subject or file name (NF-03).
"""

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import Engine

from argus.auth import Role, SignedInUser, require_role
from argus.auth.tokens import LOCAL_HOSTS, TokenVerifier
from argus.documents.store import DocumentStore
from argus.queue_page.views import (
    ExceptionDetail,
    QueueRow,
    email_files,
    open_exception_detail,
    queue_rows,
    read_email,
)

log = logging.getLogger(__name__)

STATIC = files("argus.queue_page") / "static"

# The page and the files it loads. Fixed files without business data, open like /health.
# The fonts are Geist and Geist Mono (SIL Open Font License, geist.LICENSE), served from here
# so the page loads nothing from another site (#152).
_ASSETS = {
    "/queue/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/queue/app.css": ("app.css", "text/css; charset=utf-8"),
    "/queue/msal-browser.min.js": ("msal-browser.min.js", "text/javascript; charset=utf-8"),
    "/queue/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/queue/favicon-16.svg": ("favicon-16.svg", "image/svg+xml"),
    "/queue/Geist-Regular.woff2": ("Geist-Regular.woff2", "font/woff2"),
    "/queue/Geist-Medium.woff2": ("Geist-Medium.woff2", "font/woff2"),
    "/queue/Geist-SemiBold.woff2": ("Geist-SemiBold.woff2", "font/woff2"),
    "/queue/GeistMono-Regular.woff2": ("GeistMono-Regular.woff2", "font/woff2"),
    "/queue/GeistMono-Medium.woff2": ("GeistMono-Medium.woff2", "font/woff2"),
    "/queue/geist.LICENSE": ("geist.LICENSE", "text/plain; charset=utf-8"),
}
PAGE_PATHS = frozenset({"/queue"} | set(_ASSETS))

# Benjamin works the queue (#18, question 6); Arita runs ARGUS. Approvers do not see it.
QueueReader = Annotated[SignedInUser, Depends(require_role(Role.FINANCE, Role.ADMIN))]

_SAFE_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")


def _page_headers() -> dict[str, str]:
    # The sign in library talks to Microsoft's sign in address, everything else stays here.
    login = "https://login.microsoftonline.com"
    return {
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; "
            f"connect-src 'self' {login}; frame-src {login}; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


def _data_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _sign_in_mode(verifier: TokenVerifier | None) -> dict[str, str]:
    """What the page needs to sign people in. None of it is secret (docs/operations/sign-in.md).
    With the local fake (standins/entra) there is no Microsoft sign in page, so the page asks
    for a token from the fake instead."""
    if verifier is None:
        return {"mode": "off"}
    s = verifier.settings
    local = any(s.authority.startswith(f"http://{host}") for host in LOCAL_HOSTS)
    return {
        "mode": "token" if local else "microsoft",
        "authority": f"{s.authority}/{s.tenant_id}",
        "client-id": s.api_client_id,
        "scope": f"api://{s.api_client_id}/{s.scope}",
    }


def render_page(verifier: TokenVerifier | None) -> str:
    meta = "\n".join(
        f'  <meta name="argus-sign-in-{html.escape(k)}" content="{html.escape(v)}">'
        for k, v in _sign_in_mode(verifier).items()
    )
    template = (STATIC / "queue.html").read_text(encoding="utf-8")
    return template.replace("  <!-- sign in settings -->", meta)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def row_json(row: QueueRow) -> dict[str, Any]:
    return {**asdict(row), "opened_at": _iso(row.opened_at)}


def detail_json(detail: ExceptionDetail) -> dict[str, Any]:
    out = asdict(detail)
    out["opened_at"] = _iso(detail.opened_at)
    out["received_at"] = _iso(detail.received_at)
    for f in out["files"]:
        f["fingerprint"] = f.pop("sha256")
    return out


def _download_name(document_id: int, kind: str, filename: str | None) -> str:
    """A header value that cannot break the response: an ASCII fallback plus the real name
    encoded as RFC 5987 allows."""
    fallback = f"argus-{document_id}.eml" if kind == "email" else f"argus-{document_id}"
    real = filename or fallback
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(real, safe='')}"


def install_queue_page(
    app: FastAPI,
    engine: Callable[[], Engine],
    documents: Callable[[], DocumentStore],
) -> None:
    @app.get("/queue", include_in_schema=False)
    def queue_page(request: Request) -> HTMLResponse:
        verifier: TokenVerifier | None = getattr(request.app.state, "token_verifier", None)
        return HTMLResponse(render_page(verifier), headers=_page_headers())

    for path, (name, media_type) in _ASSETS.items():

        def asset(name: str = name, media_type: str = media_type) -> Response:
            body = (STATIC / name).read_bytes()
            return Response(body, media_type=media_type, headers=_page_headers())

        app.add_api_route(path, asset, methods=["GET"], include_in_schema=False)

    @app.get("/exceptions")
    def list_exceptions(user: QueueReader) -> Response:
        """Every open exception, oldest first (EX-01)."""
        with engine().connect() as connection:
            rows = queue_rows(connection)
        log.info("user %s listed the exception queue (%d open)", user.object_id, len(rows))
        return _json({"exceptions": [row_json(r) for r in rows]})

    @app.get("/exceptions/{exception_id}")
    def read_exception(exception_id: int, user: QueueReader) -> Response:
        """One open exception: why ARGUS stopped, the case and the original email (EX-02)."""
        with engine().connect() as connection:
            detail = open_exception_detail(connection, exception_id)
        if detail is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such open exception")
        body = detail_json(detail)
        body["email"], body["email_problem"] = None, None
        raw = next((f for f in detail.files if f.kind == "email"), None)
        if raw is not None:
            try:
                body["email"] = asdict(read_email(documents().get(raw.sha256)))
            except Exception as problem:
                # The type of problem only: its message could name the file (NF-03).
                log.error(
                    "exception %s: stored email %s could not be read (%s)",
                    exception_id,
                    raw.document_id,
                    type(problem).__name__,
                )
                body["email_problem"] = "The stored email could not be loaded right now."
        elif detail.email_id is not None:
            body["email_problem"] = "No stored copy of this email is recorded."
        log.info("user %s opened exception %s", user.object_id, exception_id)
        return _json(body)

    @app.get("/exceptions/{exception_id}/files/{document_id}")
    def download_file(exception_id: int, document_id: int, user: QueueReader) -> Response:
        """One file of the exception's email, exactly as stored (#20). Always as a download,
        never shown inline, so an HTML file from a sender cannot run in this site."""
        not_found = HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
        with engine().connect() as connection:
            detail = open_exception_detail(connection, exception_id)
            if detail is None or detail.email_id is None:
                raise not_found
            found = next(
                (
                    f
                    for f in email_files(connection, detail.email_id, None)
                    if f.document_id == document_id
                ),
                None,
            )
        if found is None:
            raise not_found
        try:
            data = documents().get(found.sha256)
        except Exception as problem:
            log.error(
                "exception %s: file %s could not be read (%s)",
                exception_id,
                document_id,
                type(problem).__name__,
            )
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "the file could not be loaded right now"
            ) from None
        content_type = (
            found.content_type.lower()
            if _SAFE_CONTENT_TYPE.match(found.content_type.lower())
            else "application/octet-stream"
        )
        log.info(
            "user %s downloaded file %s of exception %s", user.object_id, document_id, exception_id
        )
        return Response(
            data,
            media_type=content_type,
            headers={
                **_data_headers(),
                "Content-Disposition": _download_name(document_id, found.kind, found.filename),
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )


def _json(body: dict[str, Any]) -> Response:
    return Response(
        json.dumps(body, default=str), media_type="application/json", headers=_data_headers()
    )
