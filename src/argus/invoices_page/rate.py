"""How many invoices from known vendors go through without manual work, per month (#64, NF-07).

The target is at least 90 % of invoices from known vendors with no manual step apart from
approval. The invoices page shows the figure for the current and the last month.

What counts, as the story defines it:

- **Invoices**: cases of kind incoming invoice. Credit notes, outgoing invoices and bank
  transactions are not part of the figure.
- **The month**: the Berlin month the email came in, or the month the case was created when it
  has no email. The same date the invoices page shows as "Received".
- **Known vendor**: the case is matched to a vendor account (``partner_account``) and never had
  an ``unknown_vendor`` exception. A case that only got its account after someone looked at it
  is not from a known vendor, so it is left out of the figure altogether.
- **No manual step**: the case never had an exception item of any reason, open or resolved.
  An exception on the email the case came from (for example an uncertain classification before
  the case existed) counts too, because a person had to act on that invoice. Approval decisions
  and questions to the approver are not exception items, so they never count.

The figure is rounded down, so it never shows 90 % for a month that is below the target.
Nothing here writes or logs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from argus.cases.model import CaseKind, ExceptionReason

BERLIN = ZoneInfo("Europe/Berlin")
TARGET_PERCENT = 90

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class MonthRate:
    month: str  # "2026-11"
    label: str  # "November 2026"
    straight: int  # cases with no manual step
    total: int  # invoices from known vendors that came in that month

    @property
    def percent(self) -> int | None:
        """Rounded down; None when no invoice came in."""
        return self.straight * 100 // self.total if self.total else None

    @property
    def below_target(self) -> bool:
        percent = self.percent
        return percent is not None and percent < TARGET_PERCENT


def _month_start(year: int, month: int) -> datetime:
    """Midnight on the first of the month in Berlin."""
    return datetime(year, month, 1, tzinfo=BERLIN)


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


_COUNT = """
    select count(*) as total,
           count(*) filter (where not exists (
             select 1 from exception_item e
             where e.case_id = c.id
                or (c.source_email_id is not null and e.email_id = c.source_email_id
                    and e.case_id is null)
           )) as straight
    from invoice_case c
    left join mailbox_email m on m.id = c.source_email_id
    where c.kind = cast(:kind as case_kind)
      and c.partner_account is not null
      and not exists (
        select 1 from exception_item u
        where u.case_id = c.id and u.reason = cast(:unknown as exception_reason)
      )
      and coalesce(m.received_at, c.created_at) >= :start
      and coalesce(m.received_at, c.created_at) < :end
"""


def month_rate(connection: Connection, year: int, month: int) -> MonthRate:
    """The figure for one Berlin month."""
    next_year, next_month = _shift(year, month, 1)
    row = (
        connection.execute(
            text(_COUNT),
            {
                "kind": CaseKind.INCOMING_INVOICE.value,
                "unknown": ExceptionReason.UNKNOWN_VENDOR.value,
                "start": _month_start(year, month),
                "end": _month_start(next_year, next_month),
            },
        )
        .mappings()
        .one()
    )
    return MonthRate(
        month=f"{year:04d}-{month:02d}",
        label=f"{MONTH_NAMES[month - 1]} {year}",
        straight=int(row["straight"]),
        total=int(row["total"]),
    )


def straight_through(connection: Connection, now: datetime | None = None) -> list[MonthRate]:
    """The current month and the last one, in that order, by the Berlin calendar."""
    here = (now or datetime.now(UTC)).astimezone(BERLIN)
    last_year, last_month = _shift(here.year, here.month, -1)
    return [
        month_rate(connection, here.year, here.month),
        month_rate(connection, last_year, last_month),
    ]
