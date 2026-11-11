"""AC-1: at least 95 % of the 50 test emails are classified and routed correctly.

Passes when at least 48 of the 50 fixture emails end in the ARGUS folder of the route that
``manifest.csv`` expects. An ambiguous email counts as correct only in manual review (IN-09).
Every invoice sent to the deletion folder is reported, because a lost invoice is the most
expensive mistake (test strategy).
"""

import pytest
from sqlalchemy import Engine, text

from argus.intake.filing import REMOVED_FOLDER
from argus.sorting import Route
from tests.acceptance.intake import IntakeRun
from tests.unit.sorting_helpers import manifest

INVOICES = {"incoming_invoice", "outgoing_invoice"}


@pytest.mark.req("AC-1", "IN-01", "IN-04", "IN-07", "IN-09")
def test_at_least_48_of_the_50_emails_end_in_the_folder_of_their_route(
    intake_run: IntakeRun,
) -> None:
    assert (intake_run.first_sweep.listed, intake_run.first_sweep.stored) == (50, 50)
    assert intake_run.second_sweep.failed == 0
    wrong = [
        (e.fixture_id, e.expected_route.value, e.decision.route.value, e.folder)
        for e in intake_run.emails
        if not e.landed_as_expected
    ]
    print(f"{50 - len(wrong)} of 50 routed as expected; wrong: {wrong or 'none'}")
    assert len(intake_run.emails) == 50
    assert len(wrong) <= 2, wrong


@pytest.mark.req("AC-1", "IN-06", "IN-09")
def test_no_invoice_is_sent_to_the_deletion_folder(intake_run: IntakeRun) -> None:
    lost = [
        e.fixture_id
        for e in intake_run.emails
        if e.expected_category in INVOICES
        and (e.decision.route is Route.DELETION_FOLDER or e.folder == REMOVED_FOLDER)
    ]
    assert lost == []


@pytest.mark.req("AC-1", "IN-01", "IN-04")
def test_the_sweep_sorts_every_email_on_its_own(intake_run: IntakeRun) -> None:
    """One sweep takes every email past the step stored, with no test stand in (#91)."""
    stuck = [i for i, step in intake_run.steps_after_first_sweep.items() if step == "stored"]
    assert stuck == []


@pytest.mark.req("AC-1", "IN-08")
def test_every_incoming_invoice_email_opens_one_case_per_invoice(
    app: Engine, intake_run: IntakeRun
) -> None:
    """Splitting the invoices into cases (#30) sends no invoice email to manual review: each
    one opens as many cases as the manifest says, E14 three of them."""
    counts = {e["email_id"]: int(e["invoice_count"]) for e in manifest()}
    with app.connect() as connection:
        opened: dict[int, int] = dict(
            connection.execute(
                text(
                    "select source_email_id, count(*) from invoice_case"
                    " where source_email_id = any(:ids) group by source_email_id"
                ),
                {"ids": [e.record_id for e in intake_run.emails]},
            ).all()
        )
    for e in intake_run.emails:
        expected = counts[e.fixture_id] if e.decision.route is Route.INVOICE_PROCESSING else 0
        assert opened.get(e.record_id, 0) == expected, e.fixture_id


@pytest.mark.req("AC-1", "IN-10")
def test_e15_the_second_copy_of_e01_is_flagged_as_a_duplicate(
    app: Engine, intake_run: IntakeRun
) -> None:
    """E15 sends invoice BB-2026-1187 of E01 again (#31). Its case waits in the exception queue
    as a duplicate, linked to the same first case as E01."""
    records = {e.fixture_id: e.record_id for e in intake_run.emails}
    with app.connect() as connection:
        cases = {
            fixture_id: connection.execute(
                text(
                    "select id, status, duplicate_of, exists (select 1 from exception_item x"
                    " where x.case_id = c.id and x.reason = 'duplicate'"
                    " and x.resolved_at is null) as flagged"
                    " from invoice_case c where source_email_id = :id and exists"
                    " (select 1 from case_document l join stored_document d on d.id = l.document_id"
                    " where l.case_id = c.id and d.email_id = :id)"
                ),
                {"id": records[fixture_id]},
            )
            .mappings()
            .one()
            for fixture_id in ("E01", "E15")
        }
    first = cases["E01"]["duplicate_of"] or cases["E01"]["id"]
    assert cases["E15"]["duplicate_of"] == first
    assert (cases["E15"]["status"], cases["E15"]["flagged"]) == ("exception", True)
