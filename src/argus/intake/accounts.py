"""Propose the G/L account and tax key from the vendor's booking history (#37, PR-04).

Approvers should not have to know the chart of accounts. Once an incoming invoice or credit
note is matched to its vendor account (#35), ARGUS reads that vendor's past postings from DATEV
through the gateway and proposes the G/L account (the contra account) and tax key of the most
recent one (:func:`propose`). The proposal is stored on the case (``account_proposal``,
migration 0019), shown on the invoices page and read by the approval (#38) and the booking
(#41) through :func:`current_proposal`.

What counts as history: postings on the vendor account whose contra account is one an invoice
can be booked to (fixed assets, goods and materials, expenses). A payment, a card clearing, a
carry forward at the start of the year or a cash discount also sits on the vendor account, but
says nothing about where the invoice belongs, so it is skipped. Which accounts count depends on
the chart of accounts (SKR03 or SKR04, #18), which is a setting:
``ARGUS_CHART_OF_ACCOUNTS``, ``SKR03`` unless set. The proposal itself only copies what the
history says, so it works with either chart.

When there is nothing to copy, ARGUS does not guess (PR-04):

- No usable posting in this year and the two before: the case goes to the exception queue as
  ``no_account_proposal``.
- The postings of the most recent day point to more than one account or tax key, for example
  an invoice split over two accounts: the same, naming the accounts.

A person (finance or admin) then enters the account and tax key on the queue page, with a
reason (:func:`enter_account`), or rejects the invoice. Every sweep also looks at these
exceptions again: once the vendor has history, for example after its first invoice is booked,
ARGUS proposes from it and resolves the exception as ``fixed_in_datev``. Nobody has to touch it.

The pass runs at the end of every live sweep, right after vendor matching. It reads DATEV once
per vendor account and only when there is a case to propose for. When DATEV does not answer,
the cases wait for the next sweep. Logs name only case IDs (NF-03).
"""

import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sqlalchemy import Connection, Engine, text

from argus.cases.model import Actor, ExceptionReason, Outcome
from argus.cases.store import get_case, open_exception, resolve_exception, unit_of_work
from argus.datev.gateway import DatevError, DatevGateway, DatevUnavailable
from argus.datev.models import Posting

log = logging.getLogger("argus.intake.accounts")

PROPOSAL_ACTOR = Actor.system("account-proposal")
CHART_ENV = "ARGUS_CHART_OF_ACCOUNTS"
# How far back the history goes: this fiscal year and the two before.
HISTORY_YEARS = 2


class Chart(StrEnum):
    """The DATEV standard chart of accounts Max ASP books with (#18; SKR03 until answered)."""

    SKR03 = "SKR03"
    SKR04 = "SKR04"


# The accounts an invoice can be booked to, per chart: fixed assets, goods and materials, and
# expenses. Everything else on a vendor account is not an invoice: cash, bank, card clearing and
# money in transit (a payment), carry forwards at the start of a year (9009), liabilities,
# private accounts and other personal accounts (code review of PR 190). Received cash discounts
# sit inside the goods class and are cut out of it. An allow list, so an account nobody thought
# of counts as "not history" rather than becoming the proposal (PR-04: never guess). The DATEV
# standard numbers; widened once Max ASP's own chart is known (#18).
_INVOICE_ACCOUNTS: dict[Chart, tuple[range, ...]] = {
    Chart.SKR03: (
        range(10, 500),  # Anlagevermögen (fixed assets)
        range(3000, 3730),  # Wareneingang (goods), up to the received discounts
        range(3740, 4000),
        range(4000, 5000),  # Betriebliche Aufwendungen (expenses)
    ),
    Chart.SKR04: (
        range(10, 1000),  # Anlagevermögen (fixed assets)
        range(5000, 5730),  # Material und Waren, up to the received discounts
        range(5740, 6000),
        range(6000, 7000),  # Betriebliche Aufwendungen (expenses)
    ),
}
# What the form tells a person the chart allows.
_ALLOWED_WORDS: dict[Chart, str] = {
    Chart.SKR03: "0010 to 0499, 3000 to 4999 without 3730 to 3739",
    Chart.SKR04: "0010 to 0999, 5000 to 6999 without 5730 to 5739",
}


def chart_from_env(environ: Mapping[str, str]) -> Chart:
    """The chart from ``ARGUS_CHART_OF_ACCOUNTS``. SKR03 when it is not set; anything else is a
    setup mistake and stops the job at start, before it proposes a wrong account."""
    value = environ.get(CHART_ENV, "").strip().upper()
    if not value:
        return Chart.SKR03
    try:
        return Chart(value)
    except ValueError:
        raise ValueError(f"{CHART_ENV} must be SKR03 or SKR04") from None


def is_invoice_account(chart: Chart, account: int) -> bool:
    """A G/L account an invoice can be booked to in this chart: a fixed asset, goods or
    materials, or an expense. Never a payment, carry forward, liability or personal account."""
    return any(account in r for r in _INVOICE_ACCOUNTS[chart])


# ------------------------------------------------------------------------------- the rule


class NoProposalReason(StrEnum):
    NO_HISTORY = "no_history"
    SEVERAL_ACCOUNTS = "several_accounts"


@dataclass(frozen=True)
class Proposed:
    gl_account: str
    tax_key: str  # blank when the posting had none, for example tax free rent
    from_document_number: str
    from_posting_date: date
    postings_used: int


@dataclass(frozen=True)
class NotProposed:
    reason: NoProposalReason
    postings_read: int
    # The accounts and tax keys of the most recent day, when they differ.
    candidates: tuple[tuple[str, str], ...] = ()


def propose(postings: Iterable[Posting], chart: Chart) -> Proposed | NotProposed:
    """The G/L account and tax key of the vendor's most recent invoice posting (PR-04), or why
    there is none. Never a guess: a tie between accounts is no proposal."""
    read = list(postings)
    usable = [p for p in read if is_invoice_account(chart, p.contra_account)]
    if not usable:
        return NotProposed(NoProposalReason.NO_HISTORY, len(read))
    latest = max(p.date for p in usable)
    on_day = [p for p in usable if p.date == latest]
    pairs = sorted({(str(p.contra_account), p.tax_key.strip()) for p in on_day})
    if len(pairs) > 1:
        return NotProposed(NoProposalReason.SEVERAL_ACCOUNTS, len(read), tuple(pairs))
    source = max(on_day, key=lambda p: p.document_number)
    return Proposed(
        gl_account=pairs[0][0],
        tax_key=pairs[0][1],
        from_document_number=source.document_number,
        from_posting_date=source.date,
        postings_used=len(usable),
    )


def history_window(today: date) -> tuple[date, date]:
    return date(today.year - HISTORY_YEARS, 1, 1), today


# ------------------------------------------------------------------------------ stored


class ProposalSource(StrEnum):
    BOOKING_HISTORY = "booking_history"
    PERSON = "person"


@dataclass(frozen=True)
class AccountProposal:
    """The proposal a case carries now, for the approval (#38) and the booking (#41)."""

    id: int
    case_id: int
    vendor_account: str
    gl_account: str
    tax_key: str
    chart: Chart
    source: ProposalSource
    from_document_number: str | None
    from_posting_date: date | None
    proposed_by_name: str | None


def current_proposal(connection: Connection, case_id: int) -> AccountProposal | None:
    """The newest proposal for the vendor account the case has now, or None."""
    row = (
        connection.execute(
            text(
                """
                select p.id, p.case_id, p.vendor_account, p.gl_account, p.tax_key, p.chart,
                       p.source, p.from_document_number, p.from_posting_date,
                       p.proposed_by_name
                from account_proposal p join invoice_case c on c.id = p.case_id
                where p.case_id = :id and p.vendor_account = c.partner_account
                order by p.id desc limit 1
                """
            ),
            {"id": case_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return AccountProposal(
        id=row["id"],
        case_id=row["case_id"],
        vendor_account=row["vendor_account"],
        gl_account=row["gl_account"],
        tax_key=row["tax_key"],
        chart=Chart(row["chart"]),
        source=ProposalSource(row["source"]),
        from_document_number=row["from_document_number"],
        from_posting_date=row["from_posting_date"],
        proposed_by_name=row["proposed_by_name"],
    )


def _store_from_history(
    db: Connection,
    case_id: int,
    vendor_account: str,
    found: Proposed,
    chart: Chart,
    exception_id: int | None,
) -> int:
    proposal_id: int = db.execute(
        text(
            """
            insert into account_proposal (case_id, exception_id, vendor_account, gl_account,
                                          tax_key, chart, source, from_document_number,
                                          from_posting_date)
            values (:case_id, :exception_id, :vendor, :gl, :tax_key, :chart, 'booking_history',
                    :doc, :day)
            returning id
            """
        ),
        {
            "case_id": case_id,
            "exception_id": exception_id,
            "vendor": vendor_account,
            "gl": found.gl_account,
            "tax_key": found.tax_key,
            "chart": chart.value,
            "doc": found.from_document_number,
            "day": found.from_posting_date,
        },
    ).scalar_one()
    return proposal_id


# ------------------------------------------------------------------------------ the pass


@dataclass
class ProposalPass:
    """What one pass did. ``failed`` counts cases that hit an error; the next pass retries."""

    proposed: int = 0
    no_proposal: int = 0
    resolved: int = 0
    failed: int = 0
    skipped_datev_unavailable: bool = False
    waiting: list[int] = field(default_factory=list)  # case IDs DATEV could not answer for


@dataclass(frozen=True)
class _Work:
    case_id: int
    vendor_account: str
    exception_id: int | None


def _work(connection: Connection, cases: Collection[int] | None) -> list[_Work]:
    """Matched incoming cases still before approval without a proposal for their vendor account
    and without an open missing account exception, plus the open missing account exceptions."""
    only = list(cases) if cases is not None else None
    rows = connection.execute(
        text(
            """
            select c.id as case_id, c.partner_account, null::bigint as exception_id
            from invoice_case c
            where c.kind in ('incoming_invoice', 'incoming_credit_note')
              and c.status in ('received', 'exception') and c.partner_account is not null
              and not exists (select 1 from account_proposal p
                              where p.case_id = c.id and p.vendor_account = c.partner_account)
              and not exists (select 1 from exception_item e
                              where e.case_id = c.id and e.resolved_at is null
                                and e.reason = 'no_account_proposal')
              and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
            union all
            select c.id, c.partner_account, e.id
            from exception_item e join invoice_case c on c.id = e.case_id
            where e.resolved_at is null and e.reason = 'no_account_proposal'
              and c.partner_account is not null
              and (cast(:only as bigint[]) is null or c.id = any(cast(:only as bigint[])))
            order by 1
            """
        ),
        {"only": only},
    ).all()
    return [_Work(r.case_id, r.partner_account, r.exception_id) for r in rows]


def propose_accounts(
    engine: Engine,
    datev: DatevGateway,
    chart: Chart,
    *,
    cases: Collection[int] | None = None,
    today: date | None = None,
) -> ProposalPass:
    """One pass: propose the G/L account and tax key for matched cases without one, and look
    again at the open missing account exceptions. ``cases`` limits the pass to these case IDs
    (tests share one database). Each case is its own unit of work, so one failure leaves the
    others alone. DATEV is read once per vendor account, and only when there is work."""
    result = ProposalPass()
    with engine.connect() as connection:
        work = _work(connection, cases)
    if not work:
        return result
    start, end = history_window(today or date.today())
    history: dict[str, list[Posting]] = {}
    for item in work:
        if item.vendor_account not in history:
            try:
                history[item.vendor_account] = datev.list_postings(
                    int(item.vendor_account), start, end
                )
            except DatevUnavailable:
                log.warning("DATEV did not answer, account proposals wait for the next sweep")
                result.skipped_datev_unavailable = True
                result.waiting += [w.case_id for w in work if w.vendor_account not in history]
                return result
            except (DatevError, ValueError):
                # A setup problem, or an account number DATEV cannot read. Never a reason to
                # call the history empty: that would send healthy invoices to the queue.
                log.exception(
                    "Reading postings failed on case %s, next sweep retries", item.case_id
                )
                result.failed += 1
                continue
        try:
            with unit_of_work(engine, PROPOSAL_ACTOR) as db:
                _handle(db, item, history[item.vendor_account], chart, result)
        except Exception:
            log.exception("Account proposal failed on case %s, next sweep retries", item.case_id)
            result.failed += 1
    return result


def _still_open(db: Connection, exception_id: int) -> bool:
    return bool(
        db.execute(
            text("select resolved_at is null from exception_item where id = :id for update"),
            {"id": exception_id},
        ).scalar()
    )


def _handle(
    db: Connection, item: _Work, postings: list[Posting], chart: Chart, result: ProposalPass
) -> None:
    case = get_case(db, item.case_id)
    if case.partner_account != item.vendor_account:
        return  # changed since the work was listed
    if item.exception_id is not None and not _still_open(db, item.exception_id):
        return  # a person resolved it a moment ago
    found = propose(postings, chart)
    if isinstance(found, NotProposed):
        if item.exception_id is None:
            open_exception(
                db,
                ExceptionReason.NO_ACCOUNT_PROPOSAL,
                _no_proposal_detail(item.vendor_account, found),
                case_id=item.case_id,
                facts={
                    "account": item.vendor_account,
                    "why": found.reason.value,
                    "postings_read": found.postings_read,
                    "chart": chart.value,
                    "candidates": [
                        {"gl_account": gl, "tax_key": key} for gl, key in found.candidates
                    ],
                },
            )
            result.no_proposal += 1
        return
    _store_from_history(db, item.case_id, item.vendor_account, found, chart, item.exception_id)
    result.proposed += 1
    if item.exception_id is not None:
        resolve_exception(
            db,
            item.exception_id,
            Outcome.FIXED_IN_DATEV,
            resolution=(
                f"Vendor account {item.vendor_account} now has booking history in DATEV. ARGUS "
                f"proposed G/L account {found.gl_account} with "
                f"{_tax_key_words(found.tax_key)} and carried on."
            ),
        )
        result.resolved += 1


def _tax_key_words(tax_key: str) -> str:
    return f"tax key {tax_key}" if tax_key else "no tax key"


def _no_proposal_detail(account: str, found: NotProposed) -> str:
    how = (
        " Enter the G/L account and tax key here, or reject the invoice. Once the vendor has "
        "booking history in DATEV, ARGUS proposes from it by itself."
    )
    if found.reason is NoProposalReason.SEVERAL_ACCOUNTS:
        listed = ", ".join(f"{gl} ({_tax_key_words(key)})" for gl, key in found.candidates)
        return (
            f"The most recent postings of vendor account {account} in DATEV went to different "
            f"accounts: {listed}. ARGUS does not guess which one this invoice belongs to.{how}"
        )
    return (
        f"Vendor account {account} has no booking history in DATEV for this year and the two "
        f"before, so ARGUS cannot propose a G/L account and tax key and does not guess.{how}"
    )


# ------------------------------------------------------------------- entered by a person


class BadAccount(ValueError):
    """A value the person can fix, with a sentence that says how."""


class NotEnterable(Exception):
    """The exception is not open, or it is not a missing account."""


@dataclass(frozen=True)
class EnteredBy:
    """The person who enters the account, as they were at that moment (ADR 0008)."""

    actor: Actor  # user:<Entra object ID>
    name: str
    sign_in_address: str | None = None


@dataclass(frozen=True)
class EnteredAccount:
    gl_account: str
    tax_key: str
    reason: str


def check_entered(chart: Chart, gl_account: str, tax_key: str, reason: str) -> EnteredAccount:
    """The form's values, or BadAccount with a sentence for the person."""
    gl = gl_account.strip()
    if not (gl.isascii() and gl.isdigit() and len(gl) == 4):
        raise BadAccount("Enter the G/L account as its four digits, for example 4930.")
    if not is_invoice_account(chart, int(gl)):
        raise BadAccount(
            f"{gl} is not an account an invoice is booked to in {chart.value}. Enter a fixed "
            f"asset, goods or expense account ({_ALLOWED_WORDS[chart]})."
        )
    key = tax_key.strip()
    if not (key == "" or (key.isascii() and key.isdigit() and len(key) <= 4)):
        raise BadAccount("Enter the tax key as one to four digits, or leave it empty.")
    why = reason.strip()
    if not 3 <= len(why) <= 500:
        raise BadAccount("Say briefly why this account is right (3 to 500 characters).")
    return EnteredAccount(gl, key, why)


@dataclass(frozen=True)
class Entered:
    case_id: int
    proposal_id: int


def enter_account(
    db: Connection,
    exception_id: int,
    entered: EnteredAccount,
    by: EnteredBy,
    chart: Chart,
) -> Entered:
    """Store the account a person entered for a missing account exception and resolve it as
    ``data_corrected``, in the caller's unit of work. The database checks again that a person
    does it, with a name and a reason, and writes ``account_proposed`` (migration 0019)."""
    row = db.execute(
        text(
            """
            select e.case_id, c.partner_account
            from exception_item e join invoice_case c on c.id = e.case_id
            where e.id = :id and e.resolved_at is null and e.reason = 'no_account_proposal'
            for update of e
            """
        ),
        {"id": exception_id},
    ).one_or_none()
    if row is None or row.partner_account is None:
        raise NotEnterable(exception_id)
    proposal_id: int = db.execute(
        text(
            """
            insert into account_proposal (case_id, exception_id, vendor_account, gl_account,
                                          tax_key, chart, source, reason, proposed_by_name,
                                          proposed_by_address)
            values (:case_id, :exception_id, :vendor, :gl, :tax_key, :chart, 'person', :reason,
                    :name, :address)
            returning id
            """
        ),
        {
            "case_id": row.case_id,
            "exception_id": exception_id,
            "vendor": row.partner_account,
            "gl": entered.gl_account,
            "tax_key": entered.tax_key,
            "chart": chart.value,
            "reason": entered.reason,
            "name": by.name,
            "address": by.sign_in_address,
        },
    ).scalar_one()
    resolve_exception(
        db,
        exception_id,
        Outcome.DATA_CORRECTED,
        resolution=(
            f"{by.name} entered G/L account {entered.gl_account} with "
            f"{_tax_key_words(entered.tax_key)}."
        ),
    )
    return Entered(row.case_id, proposal_id)
