"""AC-1: at least 95 % of the 50 test emails are classified and routed correctly.

Passes when at least 48 of the 50 fixture emails end in the ARGUS folder of the route that
``manifest.csv`` expects. An ambiguous email counts as correct only in manual review (IN-09).
Every invoice sent to the deletion folder is reported, because a lost invoice is the most
expensive mistake (test strategy).
"""

import pytest

from argus.intake.filing import REMOVED_FOLDER
from argus.sorting import Route
from tests.acceptance.intake import IntakeRun

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
