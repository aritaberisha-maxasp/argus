"""Tell Benjamin about statements: a balance that does not add up, and a missing statement.

Covers BA-01 and BA-08 (issue #56). Three kinds of notice, each an email with a fixed German
text and no attachment, to the one address set for payment reminders
(``ARGUS_REMINDER_FORWARD_TO``, Benjamin, IN-05). The Graph client refuses any other recipient.

- ``balance_difference``: a statement was blocked because its balances do not add up. One per
  statement. The same text is on the exception in the queue.
- ``statement_missing``: on the 2nd working day of the month, the statement of the month before
  is still missing for at least one Wise balance. One per month, naming every missing balance.
- ``statement_overdue``: the same on the 5th of the month, or the working day before it if the
  5th is not a working day, because the import must be finished by the 5th (BA-01). One per
  month. If the job did not run in between, only this one goes out.

Each notice is recorded as ``pending`` before it is sent and marked ``sent`` right after. The
database allows one notice per statement and one per kind and month, so a second run never
queues a second one. A crash between sending and marking can send one notice twice on the next
run; that is the safe side for a reminder, and nothing is ever lost.

In shadow mode (#72) ARGUS sends nothing: the notice ends as ``not_sent_shadow``. The database
writes the audit events ``bank_notice_sent`` and ``bank_notice_not_sent`` (migration 0014).
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor
from argus.cases.store import unit_of_work
from argus.intake.mode import is_live
from argus.working_days import add_working_days, berlin_date, working_day_on_or_before

log = logging.getLogger("argus.bank.notices")

NOTICE_ACTOR = Actor.system("bank-notices")
DEADLINE_DAY = 5  # the import of a month is finished by the 5th of the next month (BA-01)
FIRST_REMINDER_WORKING_DAY = 2

MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


class NoticeKind(StrEnum):
    BALANCE_DIFFERENCE = "balance_difference"
    STATEMENT_MISSING = "statement_missing"
    STATEMENT_OVERDUE = "statement_overdue"


class Notifier(Protocol):
    """Sends one email with a fixed text to Benjamin. :class:`argus.intake.graph.GraphClient`
    is the real one (``send_notice``)."""

    def send_notice(self, subject: str, text: str) -> None: ...


def month_name(period: date) -> str:
    return f"{MONTHS[period.month - 1]} {period.year}"


def previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def first_reminder_day(month: date) -> date:
    """The 2nd working day of ``month``."""
    return add_working_days(month - timedelta(days=1), FIRST_REMINDER_WORKING_DAY)


def deadline_day(month: date) -> date:
    """The 5th of ``month``, or the working day before it."""
    return working_day_on_or_before(month.replace(day=DEADLINE_DAY))


_INSERT_NOTICE = (
    "insert into bank_notice (kind, period, currencies, statement_id)"
    " values (:kind, :period, :currencies, :statement_id)"
)
_QUEUE_MONTHLY = (
    _INSERT_NOTICE
    + " on conflict (kind, period) where statement_id is null do nothing returning id"
)
_QUEUE_FOR_STATEMENT = (
    _INSERT_NOTICE
    + " on conflict (statement_id) where statement_id is not null do nothing returning id"
)


def queue_notice(
    db: Connection,
    kind: NoticeKind,
    period: date,
    currencies: Sequence[str],
    *,
    statement_id: int | None = None,
) -> int | None:
    """Record a notice as pending. Returns its ID, or None when it was queued before."""
    notice_id: int | None = db.execute(
        text(_QUEUE_MONTHLY if statement_id is None else _QUEUE_FOR_STATEMENT),
        {
            "kind": kind.value,
            "period": period,
            "currencies": sorted(currencies),
            "statement_id": statement_id,
        },
    ).scalar_one_or_none()
    return notice_id


def compose(
    db: Connection,
    kind: NoticeKind,
    period: date,
    currencies: Sequence[str],
    statement_id: int | None,
) -> tuple[str, str]:
    """Subject and text of a notice. Fixed wording; only currency, month and amounts vary."""
    month = month_name(period)
    balances = ", ".join(currencies)
    if kind is NoticeKind.BALANCE_DIFFERENCE:
        detail = db.execute(
            text(
                "select detail from exception_item where statement_id = :id"
                " and reason = 'bank_balance_difference' order by id desc limit 1"
            ),
            {"id": statement_id},
        ).scalar_one_or_none()
        return (
            f"ARGUS: Wise Kontoauszug {balances} {month} nicht importiert",
            (detail or f"Der Wise Kontoauszug {balances} für {month} wurde nicht importiert.")
            + "\n\nDer Auszug steht in der Ausnahmeliste von ARGUS. Diese Nachricht wurde von "
            "ARGUS automatisch gesendet.",
        )
    if kind is NoticeKind.STATEMENT_MISSING:
        return (
            f"ARGUS: Wise Kontoauszug {month} fehlt",
            f"Für {month} fehlt noch der Wise Kontoauszug für: {balances}. Bitte den Auszug "
            "als CSV an das Buchhaltungspostfach senden. Der Import muss bis zum "
            f"{DEADLINE_DAY}. abgeschlossen sein.\n\nDiese Nachricht wurde von ARGUS "
            "automatisch gesendet.",
        )
    return (
        f"ARGUS: Wise Kontoauszug {month} überfällig",
        f"Der Wise Kontoauszug für {month} fehlt heute noch für: {balances}. Der Import muss "
        f"bis zum {DEADLINE_DAY}. abgeschlossen sein. Bitte den Auszug heute als CSV an das "
        "Buchhaltungspostfach senden.\n\nDiese Nachricht wurde von ARGUS automatisch gesendet.",
    )


def send_pending_notices(engine: Engine, notifier: Notifier) -> int:
    """Send every pending notice, oldest first. Returns how many were sent. A notice that
    fails to send stays pending and the next run tries again."""
    with engine.connect() as connection:
        pending = connection.execute(
            text(
                "select id, kind, period, currencies, statement_id from bank_notice"
                " where status = 'pending' order by id"
            )
        ).all()
    sent = 0
    for row in pending:
        kind = NoticeKind(row.kind)
        currencies = [c.strip() for c in row.currencies]
        if not is_live(engine):
            _finish(engine, row.id, "not_sent_shadow")
            log.info("Notice %s not sent, ARGUS runs in shadow mode", row.id)
            continue
        with engine.connect() as connection:
            subject, body = compose(connection, kind, row.period, currencies, row.statement_id)
        try:
            notifier.send_notice(subject, body)  # outside any transaction
        except Exception:
            log.exception("Notice %s could not be sent, the next run tries again", row.id)
            continue
        _finish(engine, row.id, "sent")
        log.info("Notice %s sent: %s", row.id, kind)
        sent += 1
    return sent


def _finish(engine: Engine, notice_id: int, status: str) -> None:
    with unit_of_work(engine, NOTICE_ACTOR) as db:
        db.execute(
            text("update bank_notice set status = :status where id = :id and status = 'pending'"),
            {"id": notice_id, "status": status},
        )


def missing_balances(db: Connection, period: date, balances: Iterable[str]) -> list[str]:
    """The Wise balances with no imported statement for ``period``."""
    rows: list[str] = list(
        db.execute(
            text("select currency from bank_statement where period = :p and outcome = 'imported'"),
            {"p": period},
        ).scalars()
    )
    return sorted(set(balances) - {c.strip() for c in rows})


def remind_missing_statements(
    engine: Engine, balances: Iterable[str], notifier: Notifier | None, *, today: date
) -> NoticeKind | None:
    """Run daily. Queues a reminder when last month's statement is missing on the 2nd working
    day, or an overdue notice on the 5th, then sends pending notices. Returns the kind queued
    now, if any (BA-01, #56 fourth acceptance criterion)."""
    day = berlin_date(today)
    month = day.replace(day=1)
    period = previous_month(day)
    if day >= deadline_day(month):
        kind: NoticeKind | None = NoticeKind.STATEMENT_OVERDUE
    elif day >= first_reminder_day(month):
        kind = NoticeKind.STATEMENT_MISSING
    else:
        kind = None
    queued = None
    if kind is not None:
        with unit_of_work(engine, NOTICE_ACTOR) as db:
            missing = missing_balances(db, period, balances)
            if missing and queue_notice(db, kind, period, missing) is not None:
                queued = kind
                log.info(
                    "Statement for %s missing for %d balances",
                    period.strftime("%Y-%m"),
                    len(missing),
                )
    if notifier is not None:
        send_pending_notices(engine, notifier)
    return queued
