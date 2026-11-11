"""Logs without email data: errors are written as their type and codes only (NF-03).

The text of an exception can carry what ARGUS was working on. A PostgreSQL error holds
``DETAIL: Failing row contains (...)`` and SQLAlchemy adds the bound parameters, so a failed
insert can put a sender address or a subject into the log (review of PR 131). The scheduled
jobs therefore write every error through :class:`SafeFormatter`: the exception type, the
SQLSTATE, schema, table, column and constraint for a database error, and the place in the
code where it was raised. Never the message text and never a value.
"""

import logging
import sys
import traceback
from types import TracebackType

from sqlalchemy.exc import DBAPIError

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


def database_error_summary(error: DBAPIError) -> str:
    """Codes and names of a database error, never values."""
    original = error.orig
    parts = [f"SQLSTATE {getattr(original, 'sqlstate', None) or 'unknown'}"]
    diag = getattr(original, "diag", None)
    for label, attribute in (
        ("schema", "schema_name"),
        ("table", "table_name"),
        ("column", "column_name"),
        ("constraint", "constraint_name"),
    ):
        value = getattr(diag, attribute, None) if diag is not None else None
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)


def error_summary(error: BaseException) -> str:
    """The type of the error, with the codes of a database error. No message text."""
    name = type(error).__name__
    if isinstance(error, DBAPIError):
        return f"{name} ({database_error_summary(error)})"
    return name


def _raised_at(tb: TracebackType | None) -> str:
    frames = traceback.extract_tb(tb)
    if not frames:
        return "unknown place"
    last = frames[-1]
    return f"{last.filename.rsplit('/', 1)[-1]}:{last.lineno} in {last.name}"


class SafeFormatter(logging.Formatter):
    """Writes an exception as its summary and where it was raised, instead of the traceback
    with the exception's text. The causes are named the same way."""

    def formatException(self, ei: ExcInfo | tuple[None, None, None]) -> str:
        error = ei[1]
        if error is None:
            return ""
        lines = [f"error: {error_summary(error)} at {_raised_at(ei[2])}"]
        cause = error.__cause__ or error.__context__
        seen = {id(error)}
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            lines.append(f"caused by: {error_summary(cause)} at {_raised_at(cause.__traceback__)}")
            cause = cause.__cause__ or cause.__context__
        return "\n".join(lines)

    def format(self, record: logging.LogRecord) -> str:
        # Another formatter may have cached the full traceback text on the record already.
        cached, record.exc_text = record.exc_text, None
        try:
            return super().format(record)
        finally:
            record.exc_text = cached

    def formatStack(self, stack_info: str) -> str:
        return ""


def use_safe_logging(level: int = logging.INFO) -> None:
    """Log to stdout through :class:`SafeFormatter`, like ``logging.basicConfig``: nothing
    changes when the root logger already has a handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeFormatter("%(asctime)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
