"""Shared setup for the acceptance tests AC-1 to AC-8 (#66, test strategy).

The acceptance tests run the fixture emails end to end through the stand ins: the fake Graph,
a real PostgreSQL and Azurite. They use the same database and Azurite fixtures as the
integration tests, so the same rule holds: skipped locally without them, a failure in CI.

A scenario whose feature is not built yet carries ``@pytest.mark.waits_on(<issue>, ...)``. It
is skipped with the reason "not built yet" and ``tools/acceptance.py`` shows it as not run,
never as passed. Whoever builds the last of those issues removes the marker, so the scenario
starts to count.

Run them all with ``python tools/acceptance.py``.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from argus.documents.store import AzureDocumentStore
from tests.acceptance.intake import IntakeRun, run_intake

# The database and Azurite fixtures of the integration tests.
from tests.integration.conftest import (  # noqa: F401
    app,
    blob_connection_string,
    database_url,
    documents,
    owner,
)

NOT_BUILT = "not built yet, waits on"


def waits_on_reason(issues: tuple[object, ...]) -> str:
    """The skip reason for a scenario that waits on these issues, such as ``#41, #42``."""
    if not issues or not all(isinstance(n, int) and n > 0 for n in issues):
        raise pytest.UsageError(f"waits_on needs one or more issue numbers, got {issues!r}")
    return f"{NOT_BUILT} " + ", ".join(f"#{n}" for n in issues)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker("waits_on")
    if marker is not None:
        pytest.skip(waits_on_reason(marker.args))


@pytest.fixture(scope="session")
def intake_run(
    owner: Engine,  # noqa: F811  the fixtures imported above
    app: Engine,  # noqa: F811
    documents: AzureDocumentStore,  # noqa: F811
) -> Iterator[IntakeRun]:
    """The 50 fixture emails taken through intake once, shared by AC-1 and AC-8."""
    yield run_intake(owner, app, documents)
