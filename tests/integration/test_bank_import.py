"""The monthly Wise statement import against a real PostgreSQL and the fake Graph (#56).

The groups follow the acceptance criteria of #56: only from Benjamin or the agreed folder and
stored unaltered with its SHA-256; each transaction a case, once; balances reconciled, a
difference blocks the import and tells Benjamin; a reminder when the statement is missing.
Covers BA-01, BA-07, BA-08, NF-01 and NF-03.
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from argus.bank.importer import (
    IMPORT_ACTOR,
    BankSettings,
    ImportOutcome,
    StatementSource,
    import_statement,
)
from argus.bank.notices import NoticeKind, remind_missing_statements, send_pending_notices
from argus.cases.model import CaseKind, CaseStatus
from argus.cases.store import CaseRuleBroken, NotAllowed, get_case, unit_of_work, verify_chain
from argus.documents.store import MemoryDocumentStore, fingerprint
from argus.intake.graph import GraphClient
from standins.graph.app import MAILBOX, create_app
from tests.unit.intake.graph_helpers import BASE_URL, TOKEN, StaticTokens, standin_transport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wise"
OCTOBER = (FIXTURES / "statement_EUR_2026-10.csv").read_bytes()
NOVEMBER = (FIXTURES / "statement_EUR_2026-11.csv").read_bytes()
BENJAMIN = "benjamin@maxasp.example"  # synthetic, like every address in the fixtures
SETTINGS = BankSettings(statement_senders=frozenset({BENJAMIN}), balances=frozenset({"EUR", "USD"}))
FROM_BENJAMIN = StatementSource.email(BENJAMIN, authenticated=True)
DECEMBER_2ND = date(2026, 12, 2)


def november_one_cent_off() -> bytes:
    """November with its opening balance one cent above October's closing balance."""
    return NOVEMBER.replace(b'"-500.00","EUR"', b'"-500.01","EUR"')


@pytest.fixture
def fake_graph() -> FastAPI:
    return create_app(token=TOKEN)


@pytest.fixture
def benjamin(fake_graph: FastAPI) -> GraphClient:
    """The Graph client with the reminder address set, so it may send notices."""
    return GraphClient(
        StaticTokens(),
        MAILBOX,
        BASE_URL,
        transport=standin_transport(fake_graph),
        sleep=lambda _: None,
        forward_to=BENJAMIN,
    )


def notices(fake_graph: FastAPI) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = fake_graph.state.mailbox.notices
    return sent


def statements(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text("select * from bank_statement order by id")).all())


def events(engine: Engine, statement_id: int) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "select event_type, details from audit_event where statement_id = :id"
                    " order by chain_seq"
                ),
                {"id": statement_id},
            ).all()
        )


pytestmark = pytest.mark.usefixtures("clean_bank")


# 1. Only from Benjamin or the agreed folder, stored unaltered with its SHA-256.


@pytest.mark.req("BA-01", "NF-01")
def test_a_statement_from_benjamin_is_stored_unaltered_with_its_fingerprint(app: Engine) -> None:
    documents = MemoryDocumentStore()

    result = import_statement(
        app, documents, SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND, filename="okt.csv"
    )

    assert result.outcome is ImportOutcome.IMPORTED
    assert result.sha256 == fingerprint(OCTOBER)
    assert documents.get(fingerprint(OCTOBER)) == OCTOBER
    [row] = statements(app)
    assert (row.sha256, row.location, row.size_bytes) == (
        fingerprint(OCTOBER),
        f"sha256/{fingerprint(OCTOBER)}",
        len(OCTOBER),
    )
    [(_, details)] = [e for e in events(app, row.id) if e[0] == "bank_statement_recorded"]
    assert details["sha256"] == fingerprint(OCTOBER)
    assert details["outcome"] == "imported"
    assert "okt.csv" not in str(details)  # file names stay out of the audit log (NF-03)


@pytest.mark.req("BA-01", "NF-01")
def test_a_statement_from_the_agreed_folder_is_accepted(app: Engine) -> None:
    result = import_statement(
        app,
        MemoryDocumentStore(),
        SETTINGS,
        StatementSource.folder("ARGUS/Kontoauszüge"),
        OCTOBER,
        today=DECEMBER_2ND,
    )

    assert result.outcome is ImportOutcome.IMPORTED


@pytest.mark.req("BA-01", "NF-01")
@pytest.mark.parametrize(
    "source",
    [
        StatementSource.email("someone@maxasp.example", authenticated=True),
        StatementSource.email(BENJAMIN, authenticated=False),  # SPF, DKIM and DMARC fail
        StatementSource.folder("Inbox"),
    ],
)
def test_a_statement_from_anyone_else_is_refused_and_not_stored(
    app: Engine, source: StatementSource
) -> None:
    documents = MemoryDocumentStore()

    result = import_statement(app, documents, SETTINGS, source, OCTOBER, today=DECEMBER_2ND)

    assert result.outcome is ImportOutcome.REFUSED
    assert result.refused_because == "unknown_source"
    assert statements(app) == []
    with pytest.raises(LookupError):
        documents.get(fingerprint(OCTOBER))
    with app.connect() as connection:
        refused: dict[str, str] = connection.execute(
            text(
                "select details from audit_event where event_type = 'bank_import_refused'"
                " order by chain_seq desc limit 1"
            )
        ).scalar_one()
    assert refused == {
        "sha256": fingerprint(OCTOBER),
        "source": source.kind.value,
        "reason": "unknown_source",
    }


@pytest.mark.req("BA-01")
def test_a_file_that_is_not_a_statement_is_refused_with_its_reason(app: Engine) -> None:
    result = import_statement(
        app,
        MemoryDocumentStore(),
        SETTINGS,
        FROM_BENJAMIN,
        b"Name,Amount\nx,1\n",
        today=DECEMBER_2ND,
    )

    assert (result.outcome, result.refused_because) == (
        ImportOutcome.REFUSED,
        "not_a_wise_statement",
    )
    assert statements(app) == []


@pytest.mark.req("EX-01", "BA-01")
@pytest.mark.parametrize(
    ("data", "reason", "words"),
    [
        # A custom date range in Wise that runs into the next month.
        (
            OCTOBER.replace(b'"30-10-2026","30-10-2026', b'"02-11-2026","02-11-2026'),
            "more_than_one_month",
            "mehr als einen Kalendermonat",
        ),
        # Sent on the last day of the month.
        (OCTOBER, "month_not_over", "noch nicht vorbei"),
        # A Wise export with a renamed column.
        (
            OCTOBER.replace(b'"Running Balance"', b'"Balance"'),
            "not_a_wise_statement",
            "Spalten",
        ),
    ],
)
def test_a_statement_from_benjamin_that_cannot_be_read_goes_to_the_queue_with_its_reason(
    app: Engine, data: bytes, reason: str, words: str
) -> None:
    email_id = 950_000 + len(reason)
    source = StatementSource.email(BENJAMIN, authenticated=True, email_id=email_id)
    today = date(2026, 10, 31) if reason == "month_not_over" else DECEMBER_2ND

    first = import_statement(app, MemoryDocumentStore(), SETTINGS, source, data, today=today)
    again = import_statement(app, MemoryDocumentStore(), SETTINGS, source, data, today=today)

    assert (first.outcome, first.refused_because) == (ImportOutcome.REFUSED, reason)
    assert again.outcome is ImportOutcome.REFUSED
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select detail, facts from exception_item where email_id = :id"
                " and reason = 'unreadable_document' and resolved_at is null"
            ),
            {"id": email_id},
        ).all()
    assert len(rows) == 1, "one exception, even when the same file comes twice"
    assert words in rows[0].detail
    assert rows[0].facts == {"bank_statement": True, "reason": reason, "sha256": fingerprint(data)}


@pytest.mark.req("EX-01")
def test_a_refused_file_from_anyone_else_opens_no_exception(app: Engine) -> None:
    source = StatementSource.email("someone@maxasp.example", authenticated=True, email_id=960_001)

    import_statement(app, MemoryDocumentStore(), SETTINGS, source, b"x", today=DECEMBER_2ND)

    with app.connect() as connection:
        count: int = connection.execute(
            text("select count(*) from exception_item where email_id = 960001")
        ).scalar_one()
    assert count == 0


# 2. Each transaction becomes a case; importing the same file again creates nothing new.


@pytest.mark.req("BA-07", "BA-01")
def test_each_transaction_becomes_a_bank_transaction_case(app: Engine) -> None:
    result = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )

    assert result.cases_created == 6
    with app.connect() as connection:
        case = get_case(connection, result.case_ids[0])
        row = connection.execute(
            text("select * from bank_transaction where case_id = :id"), {"id": case.id}
        ).one()
    assert (case.kind, case.status, case.gross_cents, case.currency) == (
        CaseKind.BANK_TRANSACTION,
        CaseStatus.RECEIVED,
        -23_800,
        "EUR",
    )
    assert (row.wise_transaction_id, row.booked_on, row.statement_id) == (
        "TRANSFER-900000001",
        date(2026, 10, 2),
        result.statement_id,
    )
    assert (row.counterparty, row.payment_reference) == ("Muster Bürobedarf GmbH", "BB-2026-1187")


@pytest.mark.req("BA-07")
def test_importing_the_same_file_again_creates_nothing_new(app: Engine) -> None:
    documents = MemoryDocumentStore()
    first = import_statement(app, documents, SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND)

    again = import_statement(app, documents, SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND)

    assert again.outcome is ImportOutcome.ALREADY_SEEN
    assert again.statement_id == first.statement_id
    assert again.cases_created == 0
    assert len(statements(app)) == 1
    with app.connect() as connection:
        assert connection.execute(text("select count(*) from bank_transaction")).scalar_one() == 6


@pytest.mark.req("BA-07")
def test_a_different_file_for_a_month_already_imported_adds_nothing(app: Engine) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )
    # Downloaded again: same rows, different bytes.
    redownloaded = OCTOBER.replace(b'"0.00",""', b'"0.00"," "', 1)

    result = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, redownloaded, today=DECEMBER_2ND
    )

    assert result.outcome is ImportOutcome.MONTH_ALREADY_IMPORTED
    assert [s.outcome for s in statements(app)] == ["imported", "month_already_imported"]
    with app.connect() as connection:
        assert connection.execute(text("select count(*) from bank_transaction")).scalar_one() == 6


@pytest.mark.req("BA-07")
def test_the_database_refuses_a_transaction_twice_and_one_without_an_imported_statement(
    app: Engine,
) -> None:
    result = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )
    insert = text(
        "insert into bank_transaction (case_id, wise_balance_currency, wise_transaction_id,"
        " booked_on, amount_minor, statement_id) values ((select max(id) from invoice_case"
        " where kind = 'bank_transaction'), 'EUR', :wise_id, :day, 1, :statement_id)"
    )

    with pytest.raises(CaseRuleBroken), unit_of_work(app, IMPORT_ACTOR) as db:
        db.execute(
            insert,
            {
                "wise_id": "TRANSFER-900000001",
                "day": date(2026, 10, 2),
                "statement_id": result.statement_id,
            },
        )
    with (
        pytest.raises(CaseRuleBroken, match="currency and month"),
        unit_of_work(app, IMPORT_ACTOR) as db,
    ):
        db.execute(
            insert,
            {"wise_id": "NEW-1", "day": date(2026, 11, 2), "statement_id": result.statement_id},
        )


# 3. Balances per currency reconciled; a difference blocks the import and tells Benjamin.


@pytest.mark.req("BA-08")
def test_the_next_month_imports_when_it_opens_with_last_months_closing(app: Engine) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )

    result = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, NOVEMBER, today=DECEMBER_2ND
    )

    assert result.outcome is ImportOutcome.IMPORTED
    assert result.cases_created == 3


@pytest.mark.req("BA-08")
@pytest.mark.usefixtures("live")
def test_a_difference_blocks_the_import_and_tells_benjamin(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )

    result = import_statement(
        app,
        MemoryDocumentStore(),
        SETTINGS,
        FROM_BENJAMIN,
        november_one_cent_off(),
        today=DECEMBER_2ND,
        notifier=benjamin,
    )

    assert result.outcome is ImportOutcome.BLOCKED
    assert result.cases_created == 0
    with app.connect() as connection:
        assert (
            connection.execute(
                text("select count(*) from bank_transaction where statement_id = :id"),
                {"id": result.statement_id},
            ).scalar_one()
            == 0
        )
        exception = connection.execute(
            text("select reason, detail, facts from exception_item where statement_id = :id"),
            {"id": result.statement_id},
        ).one()
    assert exception.reason == "bank_balance_difference"
    assert "Anfangssaldo 14.209,87 EUR" in exception.detail
    assert exception.facts["checks"] == ["opening_balance"]
    [sent] = notices(fake_graph)
    assert sent["toRecipients"] == [{"emailAddress": {"address": BENJAMIN}}]
    assert "11/2026" in sent["body"]["content"]
    assert "nicht importiert" in sent["subject"]
    kinds = [e[0] for e in events(app, result.statement_id or 0)]
    assert {"bank_statement_recorded", "exception_opened", "bank_notice_sent"} <= set(kinds)


@pytest.mark.req("BA-08")
@pytest.mark.usefixtures("live")
def test_the_corrected_statement_imports_and_closes_the_difference(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )
    blocked = import_statement(
        app,
        MemoryDocumentStore(),
        SETTINGS,
        FROM_BENJAMIN,
        november_one_cent_off(),
        today=DECEMBER_2ND,
        notifier=benjamin,
    )
    # The same wrong file again: nothing new, no second email.
    again = import_statement(
        app,
        MemoryDocumentStore(),
        SETTINGS,
        FROM_BENJAMIN,
        november_one_cent_off(),
        today=DECEMBER_2ND,
        notifier=benjamin,
    )

    corrected = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, NOVEMBER, today=DECEMBER_2ND
    )

    assert again.outcome is ImportOutcome.ALREADY_SEEN
    assert len(notices(fake_graph)) == 1
    assert corrected.outcome is ImportOutcome.IMPORTED
    with app.connect() as connection:
        outcome: str = connection.execute(
            text("select outcome from exception_item where statement_id = :id"),
            {"id": blocked.statement_id},
        ).scalar_one()
    assert outcome == "data_corrected"


@pytest.mark.req("BA-08")
def test_an_earlier_month_is_blocked_once_a_later_one_is_in(app: Engine) -> None:
    # November first: it opens with 14,209.86 but there is nothing to compare with yet, so it
    # becomes the starting point. Then October cannot follow, because a later month is in.
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, NOVEMBER, today=DECEMBER_2ND
    )

    october = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )

    assert october.outcome is ImportOutcome.BLOCKED
    assert [d.check.value for d in october.differences] == ["later_month_imported"]


@pytest.mark.req("BA-08")
def test_the_database_refuses_an_imported_month_that_does_not_follow_on(owner: Engine) -> None:
    row = {
        "location": "sha256/" + "a" * 64,
        "sha": "a" * 64,
    }
    insert = text(
        "insert into bank_statement (sha256, location, size_bytes, source, currency, period,"
        " opening_minor, closing_minor, transaction_count, outcome) values (:sha, :location, 1,"
        " 'folder', 'USD', :period, :opening, :closing, 1, 'imported')"
    )
    with unit_of_work(owner, IMPORT_ACTOR) as db:
        db.execute(insert, {**row, "period": date(2026, 9, 1), "opening": 0, "closing": 500})

    with pytest.raises(CaseRuleBroken, match="opens at"), unit_of_work(owner, IMPORT_ACTOR) as db:
        db.execute(
            insert,
            {
                "sha": "b" * 64,
                "location": "sha256/" + "b" * 64,
                "period": date(2026, 10, 1),
                "opening": 499,
                "closing": 600,
            },
        )


@pytest.mark.req("NF-01")
def test_statements_cannot_be_changed_or_removed_and_their_events_not_faked(app: Engine) -> None:
    result = import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=DECEMBER_2ND
    )

    with pytest.raises(NotAllowed), unit_of_work(app, IMPORT_ACTOR) as db:
        db.execute(
            text("update bank_statement set closing_minor = 0 where id = :id"),
            {"id": result.statement_id},
        )
    with pytest.raises(NotAllowed), unit_of_work(app, IMPORT_ACTOR) as db:
        db.execute(text("delete from bank_statement where id = :id"), {"id": result.statement_id})
    with (
        pytest.raises(CaseRuleBroken, match="written by the database"),
        unit_of_work(app, IMPORT_ACTOR) as db,
    ):
        db.execute(
            text(
                "insert into audit_event (statement_id, event_type) values (:id,"
                " 'bank_statement_recorded')"
            ),
            {"id": result.statement_id},
        )
    with app.connect() as connection:
        assert verify_chain(connection) is None


# 4. Reminders when the statement is missing.


@pytest.mark.req("BA-01")
@pytest.mark.usefixtures("live")
def test_benjamin_is_reminded_on_the_2nd_working_day_and_again_on_the_5th(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=date(2026, 11, 2)
    )

    # Mon 2 Nov is the 1st working day: too early.
    assert (
        remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 2)) is None
    )
    # Tue 3 Nov: EUR is in, USD is missing.
    assert (
        remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 3))
        is NoticeKind.STATEMENT_MISSING
    )
    # The job runs again the same day and the next: no second reminder.
    remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 3))
    remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 4))
    # Thu 5 Nov, the deadline.
    assert (
        remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 5))
        is NoticeKind.STATEMENT_OVERDUE
    )

    subjects = [n["subject"] for n in notices(fake_graph)]
    assert subjects == [
        "ARGUS: Wise Kontoauszug Oktober 2026 fehlt",
        "ARGUS: Wise Kontoauszug Oktober 2026 überfällig",
    ]
    assert "USD" in notices(fake_graph)[0]["body"]["content"]
    assert "EUR" not in notices(fake_graph)[0]["body"]["content"]


@pytest.mark.req("BA-01")
@pytest.mark.usefixtures("live")
def test_no_reminder_when_every_statement_is_in(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    import_statement(
        app, MemoryDocumentStore(), SETTINGS, FROM_BENJAMIN, OCTOBER, today=date(2026, 11, 2)
    )

    queued = remind_missing_statements(app, frozenset({"EUR"}), benjamin, today=date(2026, 11, 5))

    assert queued is None
    assert notices(fake_graph) == []


@pytest.mark.req("BA-01")
def test_in_shadow_mode_nothing_is_sent_to_benjamin(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    remind_missing_statements(app, SETTINGS.balances, benjamin, today=date(2026, 11, 3))

    assert notices(fake_graph) == []
    with app.connect() as connection:
        status: str = connection.execute(text("select status from bank_notice")).scalar_one()
    assert status == "not_sent_shadow"


@pytest.mark.req("BA-01")
@pytest.mark.usefixtures("live")
def test_a_notice_that_fails_to_send_stays_pending_and_goes_out_next_time(
    app: Engine, benjamin: GraphClient, fake_graph: FastAPI
) -> None:
    class Broken:
        def send_notice(self, subject: str, text: str) -> None:
            raise RuntimeError("Graph is down")

    remind_missing_statements(app, SETTINGS.balances, Broken(), today=date(2026, 11, 3))
    assert notices(fake_graph) == []

    assert send_pending_notices(app, benjamin) == 1
    assert len(notices(fake_graph)) == 1
