"""AC-8: every test case can be traced from receipt to payment in the audit log.

For every email of AC-1 the audit log shows each intake step with its time and actor, in order
and without gaps, and the hash chain is intact (NF-01, NF-05). Every case intake opens shows its
trail, the one the invoices page shows and exports (#63), from its email to its status now.
Tracing the cases of AC-2 to AC-7 on to payment waits on those scenarios.
"""

import pytest
from sqlalchemy import Engine, text

from argus.cases.store import verify_chain
from argus.invoices_page.trail import case_trail
from tests.acceptance.intake import SORTING_STANDIN, IntakeRun

INTAKE_STEPS = ["email_seen", "email_stored", "email_classified", "email_routed", "email_filed"]


@pytest.mark.req("AC-8", "NF-01", "NF-05")
def test_every_email_shows_each_intake_step_with_time_and_actor(
    app: Engine, intake_run: IntakeRun
) -> None:
    gaps = {}
    with app.connect() as connection:
        for each in intake_run.emails:
            events = connection.execute(
                text(
                    "select event_type, occurred_at, actor_type, actor from audit_event"
                    " where email_id = :id and event_type = any(:steps) order by chain_seq"
                ),
                {"id": each.record_id, "steps": INTAKE_STEPS},
            ).all()
            times = [e.occurred_at for e in events]
            if (
                [e.event_type for e in events] != INTAKE_STEPS
                or times != sorted(times)
                or not all(e.actor_type and e.actor for e in events)
            ):
                gaps[each.fixture_id] = [e.event_type for e in events]
        assert verify_chain(connection) is None
    assert gaps == {}


@pytest.mark.req("AC-8", "NF-01")
def test_no_intake_step_in_the_trail_was_recorded_by_the_test_stand_in(
    app: Engine, intake_run: IntakeRun
) -> None:
    """Since #91 the sweep records classified and routed itself, so every step in the trail
    above was taken by ARGUS, none by a test stand in."""
    with app.connect() as connection:
        by_standin = connection.execute(
            text(
                "select count(*) from audit_event"
                " where email_id = any(:ids) and actor_type = 'system' and actor = :actor"
            ),
            {"ids": [e.record_id for e in intake_run.emails], "actor": SORTING_STANDIN.name},
        ).scalar()
    assert by_standin == 0


@pytest.mark.req("AC-8", "NF-01", "NF-05")
def test_every_case_intake_opens_is_traced_from_its_email_to_its_status(
    app: Engine, intake_run: IntakeRun
) -> None:
    """The trail of each case starts with its email being found and stored, then the case
    opening, and ends with the status the case has now. Every step has a time and an actor."""
    problems = {}
    with app.connect() as connection:
        cases = connection.execute(
            text(
                "select id, status from invoice_case where source_email_id = any(:ids) order by id"
            ),
            {"ids": [e.record_id for e in intake_run.emails]},
        ).all()
        assert cases, "intake opened no case"
        for row in cases:
            case_id, status = row.id, row.status
            trail = case_trail(connection, case_id)
            assert trail is not None
            kinds = [s.event_type for s in trail.steps]
            statuses = [s.to_status for s in trail.steps if s.to_status is not None]
            times = [s.occurred_at for s in trail.steps]
            if (
                kinds[:2] != ["email_seen", "email_stored"]
                or "case_created" not in kinds
                or not statuses
                or statuses[-1] != status
                or times != sorted(times)
                or not all(s.actor_kind and s.actor for s in trail.steps)
            ):
                problems[case_id] = kinds
    assert problems == {}


@pytest.mark.req("AC-8", "NF-05")
@pytest.mark.waits_on(35, 36, 39, 41, 42, 46, 47, 48, 50, 52, 57, 58)
def test_every_case_of_ac2_to_ac7_is_traced_from_receipt_to_its_last_status() -> None:
    """Read the trail of every case the other scenarios create (``case_trail``, #63): each
    status from received to its last one (paid, closed or exception), each with time and actor,
    none missing. Waits on the issues the scenarios of AC-2 to AC-7 wait on."""
    pytest.fail("write this scenario when AC-2 to AC-7 run")
