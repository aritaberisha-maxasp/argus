"""Read a Wise balance statement CSV and check that its balances add up (BA-01, BA-07, BA-08).

Benjamin downloads one statement per Wise balance and month (ADR 0004). Wise writes one row per
transaction with its ID, date, amount, currency and the running balance after it. The CSV has
no separate opening or closing line, so they come from the rows: the opening balance is the
running balance of the first transaction minus its amount, the closing balance is the running
balance of the last one.

What makes a statement add up:

- every row's running balance is the one before it plus its amount, to the cent, so opening
  balance plus all transactions equals the closing balance, and
- its opening balance equals the closing balance of the last imported month before it in the
  same currency. The first statement of a currency has nothing to compare with and becomes the
  starting point (assumption on #56, question 3).

This module only reads and compares. It touches no database and no file store, so every rule
is tested on its own. :mod:`argus.bank.importer` records the outcome.

Amounts are whole cents (:mod:`argus.cases.money`). Wise names, payment references and
descriptions are kept for the matching stories, but they never go into the audit log or the
application log (NF-03).
"""

import csv
import hashlib
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from argus.cases.money import from_cents, to_cents

# Wise's column names in the balance statement CSV.
ID = "TransferWise ID"
DATE = "Date"
DATE_TIME = "Date Time"
AMOUNT = "Amount"
CURRENCY = "Currency"
RUNNING_BALANCE = "Running Balance"
DESCRIPTION = "Description"
REFERENCE = "Payment Reference"
PAYER = "Payer Name"
PAYEE = "Payee Name"
MERCHANT = "Merchant"
REQUIRED_COLUMNS = (ID, DATE, AMOUNT, CURRENCY, RUNNING_BALANCE)

# A month of Max ASP's Wise traffic is a few hundred rows. Anything far larger is not a
# statement, and is refused before it fills memory.
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
MAX_TEXT = 500  # longest name, reference or description kept

_AMOUNT = re.compile(r"-?\d{1,15}(\.\d{1,2})?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y")
_DATE_TIME_FORMATS = (
    "%d-%m-%Y %H:%M:%S.%f",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


class Unreadable(StrEnum):
    """Why a file is not a statement ARGUS can import. The value goes into the audit log."""

    TOO_LARGE = "too_large"
    NOT_TEXT = "not_text"
    NOT_A_WISE_STATEMENT = "not_a_wise_statement"
    NO_TRANSACTIONS = "no_transactions"
    BAD_ROW = "bad_row"
    SAME_ID_TWICE = "same_id_twice"
    MORE_THAN_ONE_CURRENCY = "more_than_one_currency"
    MORE_THAN_ONE_MONTH = "more_than_one_month"
    MONTH_NOT_OVER = "month_not_over"


class StatementUnreadable(ValueError):
    """The file is not a Wise statement ARGUS can import. ``reason`` is safe for the audit log;
    the message is for a person and names a row number at most, never its content."""

    def __init__(self, reason: Unreadable, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class WiseTransaction:
    wise_id: str
    booked_on: date
    amount_cents: int
    running_balance_cents: int
    counterparty: str | None = None
    reference: str | None = None
    description: str | None = None
    at: datetime | None = None  # Wise's date and time, used only to put the rows in order


@dataclass(frozen=True)
class WiseStatement:
    currency: str
    period: date  # the first day of the month the statement covers
    transactions: tuple[WiseTransaction, ...]  # oldest first

    @property
    def opening_cents(self) -> int:
        first = self.transactions[0]
        return first.running_balance_cents - first.amount_cents

    @property
    def closing_cents(self) -> int:
        return self.transactions[-1].running_balance_cents


class Check(StrEnum):
    """Which balance rule failed. The value goes into the audit log."""

    RUNNING_BALANCE = "running_balance"
    OPENING_BALANCE = "opening_balance"
    LATER_MONTH_IMPORTED = "later_month_imported"


@dataclass(frozen=True)
class Difference:
    """One balance that does not add up. ``row`` is the position in the statement, oldest
    first, counting from 1, for a running balance; None for the opening balance."""

    check: Check
    expected_cents: int
    found_cents: int
    row: int | None = None

    @property
    def difference_cents(self) -> int:
        return self.found_cents - self.expected_cents

    def describe(self, currency: str) -> str:
        """For the person in the exception queue and the email to Benjamin, in German."""
        found = _money(self.found_cents, currency)
        expected = _money(self.expected_cents, currency)
        if self.check is Check.RUNNING_BALANCE:
            return (
                f"Zeile {self.row}: laufender Saldo {found}, aus der Zeile davor und dem Betrag "
                f"ergeben sich {expected}"
            )
        if self.check is Check.OPENING_BALANCE:
            return (
                f"Anfangssaldo {found}, der Endsaldo des zuletzt importierten Monats ist {expected}"
            )
        return "Ein späterer Monat dieses Kontos ist schon importiert"


def _money(cents: int, currency: str) -> str:
    return (
        f"{from_cents(cents):,.2f} {currency}".replace(",", "X").replace(".", ",").replace("X", ".")
    )


def month_of(day: date) -> date:
    return day.replace(day=1)


def _decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise StatementUnreadable(Unreadable.TOO_LARGE, "the file is too large for a statement")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StatementUnreadable(Unreadable.NOT_TEXT, "the file is not UTF-8 text") from None


def _date(value: str, row: int) -> date:
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise StatementUnreadable(Unreadable.BAD_ROW, f"row {row}: the date cannot be read")


def _date_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    for pattern in _DATE_TIME_FORMATS:
        try:
            return datetime.strptime(value.strip(), pattern)
        except ValueError:
            continue
    return None


def _cents(value: str, row: int, column: str) -> int:
    text = value.strip()
    if not _AMOUNT.fullmatch(text):
        raise StatementUnreadable(Unreadable.BAD_ROW, f"row {row}: {column} is not an amount")
    return to_cents(text)


def _text(row: Mapping[str, str | None], *columns: str) -> str | None:
    for column in columns:
        value = (row.get(column) or "").strip()
        if value:
            return value[:MAX_TEXT]
    return None


def derived_id(row: Mapping[str, str | None]) -> str:
    """An ID for a row that has none (design note, BA-07): built from date, amount, currency,
    counterparty, reference and the running balance after it. The running balance keeps two
    equal payments on the same day apart."""
    parts = [
        (row.get(column) or "").strip()
        for column in (DATE, AMOUNT, CURRENCY, PAYER, PAYEE, MERCHANT, REFERENCE, RUNNING_BALANCE)
    ]
    return "derived-" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _in_order(rows: list[WiseTransaction]) -> list[WiseTransaction]:
    """Oldest first. Wise lists the newest first; a file sorted the other way is kept.

    Rows are sorted by Wise's time when every row has one, otherwise by date. The sort is
    stable, so rows with the same time (or the same date) keep the order chosen first: a
    newest first file is turned round before sorting, and when the first and last row share
    the same time or date, the order whose running balances add up wins (QA on PR 162)."""
    timed = all(r.at is not None for r in rows)

    def key(row: WiseTransaction) -> datetime:
        if timed and row.at is not None:
            return row.at
        return datetime.combine(row.booked_on, time.min)

    first, last = key(rows[0]), key(rows[-1])
    if first > last:
        rows = list(reversed(rows))
    elif first == last:
        backwards = list(reversed(rows))
        if running_balance_differences(rows) and not running_balance_differences(backwards):
            rows = backwards
    return sorted(rows, key=key)


def read_wise_csv(data: bytes, *, today: date) -> WiseStatement:
    """Read one Wise balance statement. Refuses anything that is not one currency and one
    finished month (``today`` is the Berlin date the import runs)."""
    reader = csv.DictReader(io.StringIO(_decode(data), newline=""))
    columns = {c.strip() for c in reader.fieldnames or [] if c}
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise StatementUnreadable(
            Unreadable.NOT_A_WISE_STATEMENT,
            "the file lacks the Wise columns " + ", ".join(missing),
        )
    rows: list[WiseTransaction] = []
    currencies: set[str] = set()
    seen: set[str] = set()
    for number, raw in enumerate(reader, start=1):
        if number > MAX_ROWS:
            raise StatementUnreadable(Unreadable.TOO_LARGE, "the file has too many rows")
        row = {(k or "").strip(): v for k, v in raw.items()}
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # an empty line at the end
        currency = (row.get(CURRENCY) or "").strip().upper()
        if not _CURRENCY.fullmatch(currency):
            raise StatementUnreadable(Unreadable.BAD_ROW, f"row {number}: no currency")
        currencies.add(currency)
        wise_id = (row.get(ID) or "").strip()[:200] or derived_id(row)
        if wise_id in seen:
            raise StatementUnreadable(
                Unreadable.SAME_ID_TWICE, f"row {number}: the same transaction ID appears twice"
            )
        seen.add(wise_id)
        rows.append(
            WiseTransaction(
                wise_id=wise_id,
                booked_on=_date(row.get(DATE) or "", number),
                amount_cents=_cents(row.get(AMOUNT) or "", number, AMOUNT),
                running_balance_cents=_cents(
                    row.get(RUNNING_BALANCE) or "", number, RUNNING_BALANCE
                ),
                counterparty=_text(row, PAYER, PAYEE, MERCHANT),
                reference=_text(row, REFERENCE),
                description=_text(row, DESCRIPTION),
                at=_date_time(row.get(DATE_TIME)),
            )
        )
    if not rows:
        raise StatementUnreadable(Unreadable.NO_TRANSACTIONS, "the statement has no transactions")
    if len(currencies) > 1:
        raise StatementUnreadable(
            Unreadable.MORE_THAN_ONE_CURRENCY,
            "the file holds more than one currency; send one statement per Wise balance",
        )
    months = {month_of(r.booked_on) for r in rows}
    if len(months) > 1:
        raise StatementUnreadable(
            Unreadable.MORE_THAN_ONE_MONTH, "the statement covers more than one calendar month"
        )
    period = months.pop()
    if month_of(today) <= period:
        raise StatementUnreadable(
            Unreadable.MONTH_NOT_OVER, "the month of this statement is not over yet"
        )
    return WiseStatement(
        currency=currencies.pop(), period=period, transactions=tuple(_in_order(rows))
    )


def running_balance_differences(rows: Iterable[WiseTransaction]) -> list[Difference]:
    """Every row whose running balance is not the one before it plus its amount."""
    found: list[Difference] = []
    previous: int | None = None
    for position, row in enumerate(rows, start=1):
        if previous is not None and row.running_balance_cents != previous + row.amount_cents:
            found.append(
                Difference(
                    Check.RUNNING_BALANCE,
                    expected_cents=previous + row.amount_cents,
                    found_cents=row.running_balance_cents,
                    row=position,
                )
            )
        previous = row.running_balance_cents
    return found


def balance_differences(
    statement: WiseStatement,
    previous_closing_cents: int | None,
    *,
    later_month_imported: bool = False,
) -> list[Difference]:
    """Everything that does not add up in this statement (BA-08). Empty means it may be
    imported. ``previous_closing_cents`` is the closing balance of the last imported month
    before it in the same currency, None when there is none yet."""
    found = running_balance_differences(statement.transactions)
    if previous_closing_cents is not None and statement.opening_cents != previous_closing_cents:
        found.insert(
            0,
            Difference(
                Check.OPENING_BALANCE,
                expected_cents=previous_closing_cents,
                found_cents=statement.opening_cents,
            ),
        )
    if later_month_imported:
        found.insert(0, Difference(Check.LATER_MONTH_IMPORTED, 0, 0))
    return found
