from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from argus.working_days import (
    BERLIN,
    add_working_days,
    berlin_date,
    is_public_holiday,
    is_working_day,
    previous_working_day,
    public_holidays,
    working_day_on_or_before,
)

# Written out by hand, not computed, so a library update that moves a holiday fails here.
# Easter Sundays: 5 Apr 2026, 28 Mar 2027, 16 Apr 2028, 1 Apr 2029, 21 Apr 2030.
BW_HOLIDAYS = {
    2026: ["01-01", "01-06", "04-03", "04-06", "05-01", "05-14", "05-25", "06-04", "10-03",
           "11-01", "12-25", "12-26"],
    2027: ["01-01", "01-06", "03-26", "03-29", "05-01", "05-06", "05-17", "05-27", "10-03",
           "11-01", "12-25", "12-26"],
    2028: ["01-01", "01-06", "04-14", "04-17", "05-01", "05-25", "06-05", "06-15", "10-03",
           "11-01", "12-25", "12-26"],
    2029: ["01-01", "01-06", "03-30", "04-02", "05-01", "05-10", "05-21", "05-31", "10-03",
           "11-01", "12-25", "12-26"],
    2030: ["01-01", "01-06", "04-19", "04-22", "05-01", "05-30", "06-10", "06-20", "10-03",
           "11-01", "12-25", "12-26"],
}  # fmt: skip


@pytest.mark.req("PY-03", "AP-05")
@pytest.mark.parametrize("year", sorted(BW_HOLIDAYS))
def test_holidays_match_the_pinned_list(year: int) -> None:
    expected = {date.fromisoformat(f"{year}-{md}") for md in BW_HOLIDAYS[year]}

    assert set(public_holidays(year)) == expected


@pytest.mark.req("PY-03")
@pytest.mark.parametrize(
    "day",
    [
        date(2026, 8, 15),  # Mariä Himmelfahrt, only Bavaria and Saarland
        date(2026, 10, 31),  # Reformationstag, not in Baden-Württemberg
        date(2026, 11, 18),  # Buß- und Bettag, only Saxony
        date(2026, 12, 24),  # Christmas Eve, not a public holiday
        date(2026, 12, 31),  # New Year's Eve, not a public holiday
    ],
)
def test_days_that_are_not_holidays_in_baden_wuerttemberg(day: date) -> None:
    assert not is_public_holiday(day)


@pytest.mark.req("PY-03", "AP-05")
@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 13), True),  # Friday
        (date(2026, 11, 14), False),  # Saturday
        (date(2026, 11, 15), False),  # Sunday
        (date(2026, 6, 4), False),  # Fronleichnam, a Thursday
        (date(2027, 1, 6), False),  # Heilige Drei Könige, a Wednesday
        (date(2026, 12, 24), True),  # Christmas Eve is a normal working day
    ],
)
def test_is_working_day(day: date, expected: bool) -> None:
    assert is_working_day(day) is expected


@pytest.mark.req("PY-03")
def test_sunday_15_november_2026_gives_friday_13() -> None:
    assert previous_working_day(date(2026, 11, 15)) == date(2026, 11, 13)
    assert working_day_on_or_before(date(2026, 11, 15)) == date(2026, 11, 13)


@pytest.mark.req("PY-03")
@pytest.mark.parametrize(
    ("due", "handover"),
    [
        (date(2026, 10, 15), date(2026, 10, 15)),  # Thursday, stays
        (date(2027, 5, 15), date(2027, 5, 14)),  # Saturday, back to Friday
        (date(2026, 4, 6), date(2026, 4, 2)),  # Easter Monday, back past Good Friday and Easter
        (date(2027, 1, 1), date(2026, 12, 31)),  # New Year on a Friday, back across the year end
        (date(2029, 12, 30), date(2029, 12, 28)),  # Sunday after the Christmas holidays
    ],
)
def test_handover_moves_to_previous_working_day(due: date, handover: date) -> None:
    assert working_day_on_or_before(due) == handover


@pytest.mark.req("PY-03")
def test_previous_working_day_is_strictly_before() -> None:
    assert previous_working_day(date(2026, 11, 13)) == date(2026, 11, 12)


@pytest.mark.req("AP-05")
@pytest.mark.parametrize(
    ("start", "days", "expected"),
    [
        # Across a weekend: Thursday 12 Nov 2026
        (date(2026, 11, 12), 3, date(2026, 11, 17)),
        (date(2026, 11, 12), 5, date(2026, 11, 19)),
        # Across New Year and Heilige Drei Könige: Wednesday 30 Dec 2026
        (date(2026, 12, 30), 3, date(2027, 1, 5)),
        (date(2026, 12, 30), 5, date(2027, 1, 8)),
        # Across Easter: Thursday 2 Apr 2026, Good Friday and Easter Monday are off
        (date(2026, 4, 2), 3, date(2026, 4, 9)),
        (date(2026, 4, 2), 5, date(2026, 4, 13)),
        # Starting on a Saturday counts from Monday
        (date(2026, 11, 14), 3, date(2026, 11, 18)),
    ],
)
def test_add_working_days(start: date, days: int, expected: date) -> None:
    assert add_working_days(start, days) == expected


@pytest.mark.req("AP-05")
@pytest.mark.parametrize("days", [0, -1])
def test_add_working_days_needs_a_positive_count(days: int) -> None:
    with pytest.raises(ValueError, match="1 or more"):
        add_working_days(date(2026, 11, 12), days)


@pytest.mark.req("PY-03", "AP-05")
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Summer time (UTC+2): 22:30 UTC on Friday 12 Jun 2026 is already Saturday in Berlin
        (datetime(2026, 6, 12, 22, 30, tzinfo=UTC), date(2026, 6, 13)),
        (datetime(2026, 6, 12, 21, 59, tzinfo=UTC), date(2026, 6, 12)),
        # Winter time (UTC+1): 23:00 UTC is midnight in Berlin
        (datetime(2026, 11, 12, 23, 0, tzinfo=UTC), date(2026, 11, 13)),
        (datetime(2026, 11, 12, 22, 59, tzinfo=UTC), date(2026, 11, 12)),
        # Switch to summer time on Sunday 29 Mar 2026 at 01:00 UTC
        (datetime(2026, 3, 28, 22, 59, tzinfo=UTC), date(2026, 3, 28)),
        (datetime(2026, 3, 28, 23, 0, tzinfo=UTC), date(2026, 3, 29)),
        (datetime(2026, 3, 29, 21, 59, tzinfo=UTC), date(2026, 3, 29)),
        (datetime(2026, 3, 29, 22, 0, tzinfo=UTC), date(2026, 3, 30)),
        # Switch back to winter time on Sunday 25 Oct 2026 at 01:00 UTC
        (datetime(2026, 10, 24, 21, 59, tzinfo=UTC), date(2026, 10, 24)),
        (datetime(2026, 10, 24, 22, 0, tzinfo=UTC), date(2026, 10, 25)),
        (datetime(2026, 10, 25, 22, 59, tzinfo=UTC), date(2026, 10, 25)),
        (datetime(2026, 10, 25, 23, 0, tzinfo=UTC), date(2026, 10, 26)),
    ],
)
def test_berlin_date_follows_summer_and_winter_time(moment: datetime, expected: date) -> None:
    assert berlin_date(moment) == expected


@pytest.mark.req("PY-03", "AP-05")
def test_late_friday_utc_in_summer_is_a_saturday_in_berlin() -> None:
    friday_late_utc = datetime(2026, 6, 12, 22, 30, tzinfo=UTC)

    assert not is_working_day(friday_late_utc)
    assert previous_working_day(friday_late_utc) == date(2026, 6, 12)


@pytest.mark.req("AP-05")
def test_reminder_clock_across_the_switch_to_winter_time() -> None:
    # Approval requested Friday 23 Oct 2026 at 18:00 Berlin (summer time)
    requested = datetime(2026, 10, 23, 18, 0, tzinfo=BERLIN)

    assert add_working_days(requested, 3) == date(2026, 10, 28)
    assert add_working_days(requested, 5) == date(2026, 10, 30)


@pytest.mark.req("AP-05")
def test_moment_in_another_zone_is_converted() -> None:
    # 19:00 Friday in New York is 01:00 Saturday in Berlin
    new_york_friday = datetime(2026, 11, 13, 19, 0, tzinfo=ZoneInfo("America/New_York"))

    assert berlin_date(new_york_friday) == date(2026, 11, 14)
    assert add_working_days(new_york_friday, 3) == date(2026, 11, 18)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        is_working_day(datetime(2026, 11, 13, 12, 0))


@pytest.mark.req("PY-03", "AP-05")
def test_every_day_2026_to_2030_has_a_nearby_working_day() -> None:
    day = date(2026, 1, 1)
    while day <= date(2030, 12, 31):
        before = working_day_on_or_before(day)
        assert is_working_day(before)
        assert timedelta(0) <= day - before <= timedelta(days=4)
        day += timedelta(days=1)
