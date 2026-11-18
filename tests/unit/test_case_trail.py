"""The words and the file of a case trail, without a database (#63, NF-01)."""

import csv
import io
from datetime import UTC, datetime

import pytest

from argus.cases.model import CaseKind, CaseStatus, ExceptionReason, Outcome
from argus.invoices_page.trail import (
    FILE_COLUMNS,
    MAX_STEPS,
    OUTCOME_TEXT,
    CaseTrail,
    TrailStep,
    berlin,
    describe,
    fact_lines,
    trail_file,
)
from argus.invoices_page.views import CaseRow
from argus.queue_page.views import REASON_TEXT

pytestmark = pytest.mark.req("NF-01", "AP-08")

CASE = CaseRow(
    case_id=7,
    kind=CaseKind.INCOMING_INVOICE,
    status=CaseStatus.BOOKED,
    status_text="Booked",
    resume_status=None,
    closed_reason=None,
    partner_name="Lieferant Test GmbH",
    partner_account=None,
    sender=None,
    document_number="R-1",
    gross_cents=11900,
    currency="EUR",
    received_at=datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
    invoice_date=None,
    due_date=None,
    updated_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
    exception_id=None,
    exception_reason=None,
    exception_text=None,
)


def step(n: int, comment: str | None = None, actor: str = "intake") -> TrailStep:
    return TrailStep(
        chain_seq=100 + n,
        occurred_at=datetime(2026, 7, 1, 10, 0, n, tzinfo=UTC),
        event_type="status_changed",
        what="The status changed from Received to Awaiting approval",
        actor_kind="system",
        actor=actor,
        actor_name=None,
        from_status=CaseStatus.RECEIVED,
        to_status=CaseStatus.AWAITING_APPROVAL,
        facts=[],
        details={"note": "x"},
        comment=comment,
        hash_hex="ab" * 32,
    )


def read(file: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(file), delimiter=";"))


def test_every_outcome_has_words() -> None:
    assert set(OUTCOME_TEXT) == set(Outcome)


@pytest.mark.parametrize(
    ("event", "before", "after", "details", "sentence"),
    [
        ("status_changed", "booked", "paid", {}, "The status changed from Booked to Paid"),
        ("case_created", None, "received", {}, "ARGUS opened the case as Received"),
        (
            "exception_opened",
            None,
            None,
            {"reason": "duplicate"},
            "An exception stopped the case: " + REASON_TEXT[ExceptionReason.DUPLICATE],
        ),
        ("email_seen", None, None, {}, "ARGUS found the email in the accounts inbox"),
        # A new kind of event still shows, in words.
        ("payment_released_somewhere", None, None, {}, "Payment released somewhere"),
        # A reason or status the page does not know yet shows in words, never an error.
        (
            "exception_opened",
            None,
            None,
            {"reason": "new_reason"},
            "An exception stopped the case: new reason",
        ),
    ],
)
def test_each_event_reads_as_a_sentence(
    event: str, before: str | None, after: str | None, details: dict[str, str], sentence: str
) -> None:
    assert describe(event, before, after, details) == sentence


def test_a_resolved_exception_names_how_it_ended() -> None:
    assert describe(
        "exception_resolved", None, None, {"reason": "booking_error", "outcome": "fixed_in_datev"}
    ).startswith("The exception was resolved as fixed in DATEV: ")


def test_details_show_a_change_with_its_old_and_new_value() -> None:
    assert fact_lines(
        {"gross_minor": {"old": None, "new": 5000}, "kind": "incoming_invoice", "facts": {"a": 1}}
    ) == ['facts: {"a": 1}', "gross minor: none → 5000", "kind: incoming_invoice"]


def test_berlin_time_has_its_offset_in_summer_and_winter() -> None:
    assert berlin(datetime(2026, 7, 1, 10, 0, tzinfo=UTC)) == "2026-07-01T12:00:00+02:00"
    assert berlin(datetime(2026, 12, 1, 10, 0, tzinfo=UTC)) == "2026-12-01T11:00:00+01:00"


def test_the_file_says_what_it_is_then_one_row_per_step() -> None:
    trail = CaseTrail(case=CASE, steps=[step(1), step(2)], truncated=False)
    rows = read(
        trail_file(
            trail,
            chain_broken_at=None,
            exported_at=datetime(2026, 11, 18, 20, 0, tzinfo=UTC),
            exported_by="Arita Test (44444444)",
        )
    )
    assert rows[0] == ["ARGUS audit trail of case 7"]
    assert rows[1] == [
        "Exported 2026-11-18T21:00:00+01:00 by Arita Test (44444444), read from the audit log"
    ]
    assert rows[2] == ["Hash chain of the whole audit log: intact"]
    assert rows[3] == ["Status now: Booked, 2 steps"]
    assert rows[4] == []
    assert rows[5] == list(FILE_COLUMNS)
    assert [r[0] for r in rows[6:]] == ["1", "2"]
    assert rows[6][1] == "2026-07-01T12:00:01+02:00"
    assert rows[6][FILE_COLUMNS.index("Details")] == '{"note": "x"}'
    assert rows[6][FILE_COLUMNS.index("Chain number")] == "101"


def test_a_broken_chain_and_a_cut_trail_are_said_plainly() -> None:
    trail = CaseTrail(case=CASE, steps=[step(1)], truncated=True)
    rows = read(
        trail_file(trail, chain_broken_at=42, exported_at=datetime.now(UTC), exported_by="someone")
    )
    assert rows[2] == ["Hash chain of the whole audit log: BROKEN at chain number 42"]
    assert rows[3] == [
        "Status now: Booked, 1 step",
        f"Only the first {MAX_STEPS} steps are in this file",
    ]


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_text_a_spreadsheet_would_run_gets_an_apostrophe(start: str) -> None:
    trail = CaseTrail(
        case=CASE, steps=[step(1, comment=start + "1+1", actor=start + "job")], truncated=False
    )
    row = read(
        trail_file(trail, chain_broken_at=None, exported_at=datetime.now(UTC), exported_by="x")
    )[6]
    assert row[FILE_COLUMNS.index("Comment")] == "'" + start + "1+1"
    assert row[FILE_COLUMNS.index("Actor")] == "'" + start + "job"


def test_semicolons_and_quotes_in_a_comment_stay_in_their_cell() -> None:
    trail = CaseTrail(case=CASE, steps=[step(1, comment='a; "b"\nc')], truncated=False)
    row = read(
        trail_file(trail, chain_broken_at=None, exported_at=datetime.now(UTC), exported_by="x")
    )[6]
    assert len(row) == len(FILE_COLUMNS)
    assert row[FILE_COLUMNS.index("Comment")] == 'a; "b"\nc'


def test_the_trail_panel_reads_only_and_has_nothing_to_type_into() -> None:
    """The trail cannot be edited from the page: its part of the page script has no field and
    asks ARGUS only with GET, and it puts server text on the page only as text."""
    from importlib.resources import files

    page = (files("argus.invoices_page") / "static" / "invoices.js").read_text(encoding="utf-8")
    start = page.index("async function trailRequest")
    part = page[start : page.index("// --- the filters", start)]
    panel = page[page.index("// --- the trail of a case") : page.index("// --- resolving an")]
    assert 'method: "GET"' in part and "POST" not in part
    for field in ('"input"', '"textarea"', '"select"', "contenteditable", "POST", "innerHTML"):
        assert field not in panel
