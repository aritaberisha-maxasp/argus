"""Demo data only in the demo database, real data never in it (#124, ADR 0009).

Every scheduled job checks this before it does anything: in demo mode the database name must
end in ``_demo``, otherwise it must not (QA on PR 130). The nightly audit copy needs it most,
because it writes into a store that may be locked for ten years (compliance review of PR 157).
"""

from collections.abc import Mapping
from typing import Final

from sqlalchemy import make_url
from sqlalchemy.exc import ArgumentError

from argus.db import DATABASE_URL_ENV

DEMO_SUFFIX: Final = "_demo"


class DatabaseModeError(ValueError):
    """The database does not fit demo mode, or is not set. The message names the setting,
    never the URL, which can hold a user name."""


def check_database_for_mode(environ: Mapping[str, str], *, demo: bool) -> None:
    """Raise :class:`DatabaseModeError` unless the database name in ``ARGUS_DATABASE_URL``
    ends in ``_demo`` exactly when ``demo`` is on."""
    url = environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise DatabaseModeError(f"set {DATABASE_URL_ENV} before the job can run")
    try:
        name = make_url(url).database or ""
    except ArgumentError:
        raise DatabaseModeError(f"{DATABASE_URL_ENV} is not a database URL") from None
    if demo and not name.endswith(DEMO_SUFFIX):
        raise DatabaseModeError(
            f"demo mode is on but the database in {DATABASE_URL_ENV} does not end in {DEMO_SUFFIX}"
        )
    if not demo and name.endswith(DEMO_SUFFIX):
        raise DatabaseModeError(
            f"demo mode is off but the database in {DATABASE_URL_ENV} ends in {DEMO_SUFFIX}"
        )
