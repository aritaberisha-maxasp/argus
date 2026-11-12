"""Working days in Baden-Württemberg (PY-03, AP-05).

A working day is Monday to Friday and not a public holiday in Baden-Württemberg.
The holidays come from the ``holidays`` library (default answer to question 2 on
issue #18); the tests pin every holiday from 2026 to 2030 so a library update
cannot change them unnoticed.

Every function takes a ``date`` or a timezone aware ``datetime``. A ``datetime`` is
first converted to its calendar day in Europe/Berlin, so a moment late on a
Friday evening UTC that is already Saturday in Germany counts as Saturday.
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import holidays

BERLIN = ZoneInfo("Europe/Berlin")

# Safety stop for the day by day searches below. The longest run of days off in
# Baden-Württemberg is four (Good Friday to Easter Monday), so this is never hit.
_MAX_DAYS_SEARCHED = 31


def berlin_date(moment: date | datetime) -> date:
    """The calendar day in Europe/Berlin. A naive ``datetime`` is refused."""
    if isinstance(moment, datetime):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("a datetime needs a timezone, ARGUS does not guess one")
        return moment.astimezone(BERLIN).date()
    return moment


@cache
def public_holidays(year: int) -> Mapping[date, str]:
    """Public holidays in Baden-Württemberg for one year, with their German names."""
    return dict(holidays.country_holidays("DE", subdiv="BW", years=year))


def is_public_holiday(day: date | datetime) -> bool:
    d = berlin_date(day)
    return d in public_holidays(d.year)


def is_working_day(day: date | datetime) -> bool:
    d = berlin_date(day)
    return d.weekday() < 5 and not is_public_holiday(d)


def _step_to_working_day(day: date, step: timedelta) -> date:
    """Walk from ``day`` in ``step`` sized steps until a working day, ``day`` included."""
    d = day
    for _ in range(_MAX_DAYS_SEARCHED):
        if is_working_day(d):
            return d
        d += step
    raise RuntimeError(f"no working day within {_MAX_DAYS_SEARCHED} days of {day}")


def working_day_on_or_before(day: date | datetime) -> date:
    """The day itself if it is a working day, otherwise the working day before it.

    This is the PY-03 rule: a payment hand-over due on a weekend or holiday moves
    to the previous working day.
    """
    return _step_to_working_day(berlin_date(day), timedelta(days=-1))


def previous_working_day(day: date | datetime) -> date:
    """The last working day strictly before the given day."""
    return working_day_on_or_before(berlin_date(day) - timedelta(days=1))


def add_working_days(start: date | datetime, days: int) -> date:
    """The day that is ``days`` working days after ``start``, not counting ``start`` itself.

    Used for the AP-05 clock: a reminder after 3 working days and an escalation
    after 5. Starting on a weekend or holiday is fine, counting begins on the next
    working day.
    """
    if days < 1:
        raise ValueError("days must be 1 or more")
    d = berlin_date(start)
    for _ in range(days):
        d = _step_to_working_day(d + timedelta(days=1), timedelta(days=1))
    return d


def working_days_between(start: date | datetime, end: date | datetime) -> int:
    """How many working days lie after ``start`` up to and including ``end``.

    The same counting as :func:`add_working_days`: ``start`` itself does not count, so
    something opened today is 0 working days old, and something opened on a Friday is 1
    working day old on Monday. An ``end`` before ``start`` gives 0. Used for the age of an
    exception in the queue (EX-01).
    """
    first, last = berlin_date(start), berlin_date(end)
    count = 0
    d = first + timedelta(days=1)
    while d <= last:
        if is_working_day(d):
            count += 1
        d += timedelta(days=1)
    return count
