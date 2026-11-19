"""A mode switch reason made only of spaces, tabs or line breaks is refused (#170, NF-01, NF-03).

Migration 0012 trimmed the reason with ``btrim``, which removes plain spaces only, so a reason of
tabs or line breaks passed and the audit log could hold an empty reason. Migration 0017 trims it
like a correction reason (``correction_trim``, migration 0013). The tests below follow the
acceptance criteria of #170. Every name and address is made up.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.app import BLANK_REASON, create_app
from argus.auth import Role
from argus.cases.store import CaseRuleBroken, unit_of_work
from argus.intake.mode import IntakeMode, SwitchedBy, is_live, switch_intake_mode
from standins.entra.app import FakeEntra
from tests.integration.conftest import ALEMBIC_INI, TEST_ADMIN, TEST_SWITCH, migrate, set_mode

pytestmark = pytest.mark.req("NF-01", "NF-03")

ARITA = "44444444-4444-4444-8444-444444444444"  # admin, made up

# Blank in every way the issue names, plus the spaces a paste can bring along.
BLANK = {
    "spaces only": "     ",
    "tabs only": "\t\t\t",
    "line breaks only": "\n\n\n",
    "windows line breaks": "\r\n\r\n",
    "mixed": " \t\n \t\n",
    "non-breaking and zero width": "\u00a0\u200b\u3000\ufeff",
}
# Long enough before trimming, too short after.
TOO_SHORT = {"one letter in tabs": "\tx\t\t", "two letters in line breaks": "\nok\n\n"}


@pytest.fixture
def shadow(owner: Engine) -> Iterator[None]:
    set_mode(owner, IntakeMode.SHADOW)
    yield
    set_mode(owner, IntakeMode.SHADOW)


@pytest.fixture(scope="module")
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def client(app: Engine, entra: FakeEntra) -> TestClient:
    return TestClient(create_app(lambda: app, sign_in=entra.verifier))


def admin(entra: FakeEntra) -> dict[str, str]:
    token = entra.token(ARITA, roles=[Role.ADMIN], name="Arita Beispiel")
    return {"Authorization": f"Bearer {token}"}


def switch_events(app: Engine) -> list[dict[str, Any]]:
    with app.connect() as connection:
        rows = connection.execute(
            text(
                "select chain_seq, details, actor from audit_event"
                " where event_type = 'intake_mode_changed' order by chain_seq"
            )
        ).mappings()
        return [dict(row) for row in rows]


def mode_row(app: Engine) -> tuple[str, str | None]:
    with app.connect() as connection:
        row = connection.execute(text("select mode, reason from intake_mode")).one()
    return row[0], row[1]


# AC: the database refuses a reason that is empty after trimming spaces, tabs and line breaks.


@pytest.mark.usefixtures("shadow")
@pytest.mark.parametrize("reason", [*BLANK.values(), *TOO_SHORT.values()], ids=[*BLANK, *TOO_SHORT])
def test_the_database_refuses_a_blank_reason(app: Engine, reason: str) -> None:
    before = switch_events(app)

    with pytest.raises(CaseRuleBroken, match="name and a reason"):
        switch_intake_mode(app, SwitchedBy(TEST_ADMIN, "Test Admin", reason), IntakeMode.LIVE)

    assert not is_live(app)
    assert switch_events(app) == before


@pytest.mark.usefixtures("shadow")
def test_the_database_keeps_a_normal_reason_trimmed(app: Engine) -> None:
    reason = " \t report of 21 Nov checked\r\n"

    assert switch_intake_mode(app, SwitchedBy(TEST_ADMIN, "Test Admin", reason), IntakeMode.LIVE)

    assert mode_row(app) == ("live", "report of 21 Nov checked")
    assert switch_events(app)[-1]["details"]["reason"] == "report of 21 Nov checked"


@pytest.mark.usefixtures("shadow")
def test_the_check_on_the_mode_row_trims_tabs_and_line_breaks_too(app: Engine) -> None:
    """Even if the guard were bypassed, the row cannot hold a blank reason."""
    with app.connect() as connection:
        found: str = connection.execute(
            text(
                "select pg_get_constraintdef(oid) from pg_constraint"
                " where conname = 'intake_mode_reason_check'"
            )
        ).scalar_one()
    assert "correction_trim(reason)" in found


# AC: the page gets a clear message instead of an error page.


@pytest.mark.usefixtures("shadow")
@pytest.mark.parametrize("reason", [*BLANK.values(), *TOO_SHORT.values()], ids=[*BLANK, *TOO_SHORT])
def test_the_switch_route_answers_a_blank_reason_with_a_sentence(
    app: Engine, client: TestClient, entra: FakeEntra, reason: str
) -> None:
    before = switch_events(app)

    refused = client.put(
        "/intake/mode", json={"mode": "live", "reason": reason}, headers=admin(entra)
    )

    assert refused.status_code == 422
    assert BLANK_REASON in str(refused.json()["detail"])
    assert not is_live(app)
    assert switch_events(app) == before


@pytest.mark.usefixtures("shadow")
def test_the_switch_route_accepts_a_normal_reason_and_logs_it_trimmed(
    app: Engine, client: TestClient, entra: FakeEntra
) -> None:
    switched = client.put(
        "/intake/mode",
        json={"mode": "live", "reason": "\tDemo fuer Benjamin\n"},
        headers=admin(entra),
    )

    assert switched.status_code == 200
    assert switched.json() == {"mode": "live", "changed": True}
    assert switch_events(app)[-1]["details"]["reason"] == "Demo fuer Benjamin"


# AC: earlier mode switches in the audit log are not changed.


@pytest.mark.usefixtures("shadow")
def test_the_migration_leaves_earlier_switches_as_they_were(owner: Engine, app: Engine) -> None:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    before_0017 = script.get_revision("0017").down_revision
    assert isinstance(before_0017, str)
    migrate(owner, before_0017, down=True)
    try:
        # Under the old rules a reason of tabs around one letter still passed.
        old = SwitchedBy(TEST_ADMIN, "Test Admin", "\t\tx\t\t")
        assert switch_intake_mode(app, old, IntakeMode.LIVE)
        written = switch_events(app)
    finally:
        migrate(owner, "head")

    # The audit log and the mode row are as they were; nothing was checked or rewritten.
    assert switch_events(app) == written
    assert written[-1]["details"]["reason"] == "\t\tx\t\t"
    assert mode_row(app) == ("live", "\t\tx\t\t")
    # The next switch follows the new rules and overwrites the row.
    with pytest.raises(CaseRuleBroken, match="name and a reason"):
        switch_intake_mode(app, old, IntakeMode.SHADOW)
    switch_intake_mode(app, TEST_SWITCH, IntakeMode.SHADOW)
    assert mode_row(app) == ("shadow", TEST_SWITCH.reason)


@pytest.mark.usefixtures("shadow")
def test_only_the_mode_change_is_still_refused_without_a_reason(app: Engine) -> None:
    """Setting the mode without who and why in the same transaction stays refused (#72)."""
    with (
        pytest.raises(CaseRuleBroken, match="name and a reason"),
        unit_of_work(app, TEST_ADMIN) as db,
    ):
        db.execute(text("select set_config('argus.switch_name', 'Test Admin', true)"))
        db.execute(text("select set_config('argus.switch_reason', E'\\t\\n\\t', true)"))
        db.execute(text("update intake_mode set mode = 'live'"))
    assert not is_live(app)
