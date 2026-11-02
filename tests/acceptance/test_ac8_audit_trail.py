"""AC-8: every test case can be traced from receipt to payment in the audit log.

For every email of AC-1 the audit log shows each intake step with its time and actor, in order
and without gaps, and the hash chain is intact (NF-01, NF-05). Tracing each case on to its last
status needs one case per invoice (#30) and the case trail (#63).
"""

import pytest
from sqlalchemy import Engine, text

from argus.cases.store import verify_chain
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


@pytest.mark.req("AC-8", "NF-05")
@pytest.mark.waits_on(30, 63)
def test_every_case_of_ac2_to_ac7_is_traced_from_receipt_to_its_last_status() -> None:
    """Read the trail of every case the other scenarios create: each status from received to
    its last one (paid, closed or exception), each with time and actor, none missing."""
    pytest.fail("write this scenario when #30 and #63 are built")
