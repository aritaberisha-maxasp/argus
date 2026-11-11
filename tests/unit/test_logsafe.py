"""Errors reach the job logs as type and codes only, never their text (NF-03, PR 131 review)."""

import logging
import sys

import pytest
from sqlalchemy.exc import IntegrityError

from argus.db import engine_from_env
from argus.logsafe import SafeFormatter, error_summary

SENDER = "erika.muster@muster-buerobedarf.example"
SUBJECT = "Rechnung 4711 Bürobedarf"


class Diag:
    schema_name = "public"
    table_name = "mailbox_email"
    column_name = None
    constraint_name = "mailbox_email_graph_message_id_key"


class PgError(Exception):
    sqlstate = "23505"
    diag = Diag()


def database_error() -> IntegrityError:
    text = f'duplicate key\nDETAIL: Failing row contains (1, "{SENDER}", "{SUBJECT}").'
    return IntegrityError("insert into mailbox_email ...", {"sender": SENDER}, PgError(text))


def formatted(error: BaseException) -> str:
    try:
        try:
            raise ValueError(f"inner {SUBJECT}")
        except ValueError as inner:
            raise error from inner
    except BaseException:
        record = logging.LogRecord(
            "argus.intake.sweep",
            logging.ERROR,
            __file__,
            1,
            "Mailbox sweep failed on email %s",
            ("AAMkAG42",),
            sys.exc_info(),
        )
    return SafeFormatter("%(name)s %(message)s").format(record)


@pytest.mark.req("NF-03")
def test_a_database_error_is_logged_as_codes_only() -> None:
    line = formatted(database_error())
    assert SENDER not in line
    assert SUBJECT not in line
    assert "Failing row" not in line
    assert "Mailbox sweep failed on email AAMkAG42" in line
    assert "IntegrityError (SQLSTATE 23505, schema public, table mailbox_email," in line
    assert "constraint mailbox_email_graph_message_id_key)" in line
    assert "caused by: ValueError at test_logsafe.py" in line


@pytest.mark.req("NF-03")
def test_any_other_error_is_logged_by_its_type_only() -> None:
    line = formatted(RuntimeError(f"could not read {SENDER}"))
    assert SENDER not in line
    assert "error: RuntimeError at test_logsafe.py" in line
    assert error_summary(KeyError(SENDER)) == "KeyError"


@pytest.mark.req("NF-03")
def test_the_engine_never_quotes_statement_values() -> None:
    engine = engine_from_env("postgresql+psycopg://argus_app@localhost/argus", environ={})
    assert engine.hide_parameters
