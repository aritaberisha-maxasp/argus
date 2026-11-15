"""Reading a Wise statement CSV and checking its balances (#56, BA-01, BA-07, BA-08).

No database: the rules of :mod:`argus.bank.statement`, the source check of the import, the
reminder days and the Graph guard for notices.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from argus.bank.importer import BankSettings, StatementSource
from argus.bank.notices import deadline_day, first_reminder_day, previous_month
from argus.bank.statement import (
    Check,
    StatementUnreadable,
    Unreadable,
    balance_differences,
    read_wise_csv,
    running_balance_differences,
)
from argus.intake.graph import notice_body_problem

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wise"
OCTOBER = (FIXTURES / "statement_EUR_2026-10.csv").read_bytes()
NOVEMBER = (FIXTURES / "statement_EUR_2026-11.csv").read_bytes()
TODAY = date(2026, 12, 2)
HEADER = '"TransferWise ID","Date","Amount","Currency","Running Balance","Payee Name"\n'
BENJAMIN = "benjamin@maxasp.example"


def csv_of(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


@pytest.mark.req("BA-01")
def test_the_october_statement_is_read_oldest_first_with_its_balances() -> None:
    statement = read_wise_csv(OCTOBER, today=TODAY)

    assert statement.currency == "EUR"
    assert statement.period == date(2026, 10, 1)
    assert [t.wise_id for t in statement.transactions][:2] == [
        "TRANSFER-900000001",
        "TRANSFER-900000002",
    ]
    assert statement.opening_cents == 1_250_000
    assert statement.closing_cents == 1_420_986
    assert statement.opening_cents + sum(t.amount_cents for t in statement.transactions) == (
        statement.closing_cents
    )


@pytest.mark.req("BA-07")
def test_names_and_references_are_kept_for_matching() -> None:
    first = read_wise_csv(OCTOBER, today=TODAY).transactions[0]

    assert first.counterparty == "Muster Bürobedarf GmbH"
    assert first.reference == "BB-2026-1187"
    assert first.amount_cents == -23_800


@pytest.mark.req("BA-08")
def test_a_statement_that_adds_up_has_no_differences() -> None:
    october = read_wise_csv(OCTOBER, today=TODAY)
    november = read_wise_csv(NOVEMBER, today=TODAY)

    assert balance_differences(october, None) == []
    assert balance_differences(november, october.closing_cents) == []


@pytest.mark.req("BA-08")
def test_an_opening_balance_that_does_not_follow_last_month_is_a_difference() -> None:
    november = read_wise_csv(NOVEMBER, today=TODAY)

    [difference] = balance_differences(november, 1_420_900)

    assert difference.check is Check.OPENING_BALANCE
    assert difference.difference_cents == 86
    assert "Anfangssaldo 14.209,86 EUR" in difference.describe("EUR")


@pytest.mark.req("BA-08")
def test_a_running_balance_one_cent_off_is_a_difference() -> None:
    data = csv_of(
        '"T-3","05-10-2026","-10.00","EUR","89.99",""',
        '"T-2","04-10-2026","50.00","EUR","100.00",""',
        '"T-1","03-10-2026","-20.00","EUR","50.00",""',
    )
    statement = read_wise_csv(data, today=TODAY)

    [difference] = running_balance_differences(statement.transactions)

    assert difference.check is Check.RUNNING_BALANCE
    assert (difference.row, difference.expected_cents, difference.found_cents) == (3, 9000, 8999)


@pytest.mark.req("BA-08")
def test_a_later_month_already_imported_blocks_an_earlier_one() -> None:
    october = read_wise_csv(OCTOBER, today=TODAY)

    differences = balance_differences(october, None, later_month_imported=True)

    assert [d.check for d in differences] == [Check.LATER_MONTH_IMPORTED]


def test_rows_of_one_day_without_times_take_the_order_that_adds_up() -> None:
    # Newest first, as Wise writes it, all on one day.
    data = csv_of(
        '"T-2","05-10-2026","5.00","EUR","15.00",""', '"T-1","05-10-2026","10.00","EUR","10.00",""'
    )

    statement = read_wise_csv(data, today=TODAY)

    assert [t.wise_id for t in statement.transactions] == ["T-1", "T-2"]
    assert statement.opening_cents == 0


@pytest.mark.req("BA-07")
def test_a_row_without_id_gets_one_built_from_its_content() -> None:
    data = csv_of(
        '"","05-10-2026","5.00","EUR","15.00","A"',
        '"","05-10-2026","5.00","EUR","10.00","A"',
    )

    ids = [t.wise_id for t in read_wise_csv(data, today=TODAY).transactions]

    assert len(set(ids)) == 2 and all(i.startswith("derived-") for i in ids)
    assert ids == [t.wise_id for t in read_wise_csv(data, today=TODAY).transactions]


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"Name,Amount\nx,1\n", Unreadable.NOT_A_WISE_STATEMENT),
        (HEADER.encode(), Unreadable.NO_TRANSACTIONS),
        (b"\xff\xfe\x00", Unreadable.NOT_TEXT),
        (csv_of('"T-1","05-10-2026","1,00","EUR","1.00",""'), Unreadable.BAD_ROW),
        (csv_of('"T-1","05-10-2026","1.001","EUR","1.00",""'), Unreadable.BAD_ROW),
        (csv_of('"T-1","2026/10/05","1.00","EUR","1.00",""'), Unreadable.BAD_ROW),
        (
            csv_of(
                '"T-1","05-10-2026","1.00","EUR","2.00",""',
                '"T-1","04-10-2026","1.00","EUR","1.00",""',
            ),
            Unreadable.SAME_ID_TWICE,
        ),
        (
            csv_of(
                '"T-2","05-10-2026","1.00","USD","2.00",""',
                '"T-1","04-10-2026","1.00","EUR","1.00",""',
            ),
            Unreadable.MORE_THAN_ONE_CURRENCY,
        ),
        (
            csv_of(
                '"T-2","01-11-2026","1.00","EUR","2.00",""',
                '"T-1","31-10-2026","1.00","EUR","1.00",""',
            ),
            Unreadable.MORE_THAN_ONE_MONTH,
        ),
        (csv_of('"T-1","01-12-2026","1.00","EUR","1.00",""'), Unreadable.MONTH_NOT_OVER),
    ],
)
def test_files_that_are_not_a_finished_wise_month_are_refused(
    data: bytes, reason: Unreadable
) -> None:
    with pytest.raises(StatementUnreadable) as refused:
        read_wise_csv(data, today=TODAY)

    assert refused.value.reason is reason


def test_a_huge_file_is_refused_before_it_is_read() -> None:
    with pytest.raises(StatementUnreadable) as refused:
        read_wise_csv(b"x" * (5 * 1024 * 1024 + 1), today=TODAY)

    assert refused.value.reason is Unreadable.TOO_LARGE


@pytest.mark.req("BA-01", "NF-01")
def test_only_benjamin_with_a_passing_sender_check_or_the_agreed_folder_counts() -> None:
    settings = BankSettings(statement_senders=frozenset({BENJAMIN}))

    assert settings.accepts(StatementSource.email("Benjamin@MaxASP.example", authenticated=True))
    assert not settings.accepts(StatementSource.email(BENJAMIN, authenticated=False))
    assert not settings.accepts(StatementSource.email("someone@maxasp.example", authenticated=True))
    assert settings.accepts(StatementSource.folder("ARGUS/Kontoauszüge"))
    assert not settings.accepts(StatementSource.folder("Inbox"))


def test_the_settings_come_from_the_environment_and_refuse_nonsense() -> None:
    settings = BankSettings.from_environment(
        {"ARGUS_STATEMENT_SENDERS": f" {BENJAMIN.upper()} ", "ARGUS_WISE_BALANCES": "eur, usd"}
    )

    assert settings.statement_senders == {BENJAMIN}
    assert settings.balances == {"EUR", "USD"}
    assert BankSettings.from_environment({}).balances == {"EUR"}
    with pytest.raises(ValueError, match="ARGUS_STATEMENT_SENDERS"):
        BankSettings.from_environment({"ARGUS_STATEMENT_SENDERS": "maxasp.example"})
    with pytest.raises(ValueError, match="ARGUS_WISE_BALANCES"):
        BankSettings.from_environment({"ARGUS_WISE_BALANCES": "EURO"})


@pytest.mark.req("BA-01")
@pytest.mark.parametrize(
    ("month", "first", "deadline"),
    [
        # 1 Nov 2026 is a Sunday and a holiday: Mon 2nd is the 1st working day.
        (date(2026, 11, 1), date(2026, 11, 3), date(2026, 11, 5)),
        # The 5th of December 2026 is a Saturday: the deadline moves to Friday the 4th.
        (date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 4)),
        # 1 Jan is a holiday, 6 Jan too (Baden-Württemberg).
        (date(2027, 1, 1), date(2027, 1, 5), date(2027, 1, 5)),
    ],
)
def test_reminder_days(month: date, first: date, deadline: date) -> None:
    assert first_reminder_day(month) == first
    assert deadline_day(month) == deadline


def test_previous_month() -> None:
    assert previous_month(date(2027, 1, 4)) == date(2026, 12, 1)


def notice(**changes: object) -> bytes:
    body: dict[str, object] = {
        "message": {
            "subject": "ARGUS: Wise Kontoauszug",
            "body": {"contentType": "Text", "content": "Text"},
            "toRecipients": [{"emailAddress": {"address": BENJAMIN}}],
        },
        "saveToSentItems": True,
    }
    message = body["message"]
    assert isinstance(message, dict)
    message.update(changes)
    return json.dumps(body).encode()


@pytest.mark.req("NF-03")
def test_a_notice_to_benjamin_with_text_only_passes_the_graph_guard() -> None:
    assert notice_body_problem(notice(), BENJAMIN) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"toRecipients": [{"emailAddress": {"address": "someone@else.example"}}]},
        {
            "toRecipients": [
                {"emailAddress": {"address": BENJAMIN}},
                {"emailAddress": {"address": "someone@else.example"}},
            ]
        },
        {"ccRecipients": [{"emailAddress": {"address": "someone@else.example"}}]},
        {"attachments": [{"name": "x.csv"}]},
        {"body": {"contentType": "HTML", "content": "<a href=x>x</a>"}},
        {"subject": ""},
        {"body": {"contentType": "Text", "content": "x" * 5001}},
    ],
)
def test_the_graph_guard_refuses_any_other_notice(changes: dict[str, object]) -> None:
    assert notice_body_problem(notice(**changes), BENJAMIN) is not None


def test_the_graph_guard_refuses_a_notice_when_no_address_is_set() -> None:
    assert notice_body_problem(notice(), "") is not None
    assert notice_body_problem(b"not json", BENJAMIN) is not None
