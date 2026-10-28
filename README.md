# ARGUS

Bookkeeping automation for Max ASP GmbH: classify and route every email in the accounts inbox, book supplier invoices, outgoing invoices and Wise bank transactions in DATEV, bundle approved invoices into Wise payment runs twice a month, and keep a full audit trail from receipt to payment.

## Run the tests

You need Python 3.12 or newer. From the repository root:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The case store tests (`tests/integration/`) need an empty PostgreSQL 16 that they may wipe. Without one they are skipped locally; in CI they always run.

```bash
docker run -d --name argus-pg -p 5432:5432 -e POSTGRES_PASSWORD=local -e POSTGRES_DB=argus_test postgres:16
ARGUS_TEST_DATABASE_URL=postgresql+psycopg://postgres:local@localhost:5432/argus_test .venv/bin/pytest
```

To create the tables in a database, set `ARGUS_DATABASE_URL` and run `.venv/bin/alembic upgrade head`. The migrations are in `src/argus/db/migrations/`.

CI runs the same tests plus `ruff check`, `ruff format --check`, `mypy`, the fixture check and a secret scan on every PR. Tag each test with the requirements it covers, for example `@pytest.mark.req("IN-04", "IN-09")`. An ID that is not in the catalog fails the run.

## Stand ins

ARGUS cannot reach Wise or DATEV from the build environment, so development and CI use stand ins that answer like the real systems. See [standins/wise/README.md](standins/wise/README.md) for the Wise one.

## Start here

- [Team charter](CLAUDE.md): what we build, how the team works, the rules
- [Requirements catalog](docs/requirements/requirements.md)
- [Workflow, board and ceremonies](docs/process/workflow.md)
- [Definition of Ready](docs/process/definition-of-ready.md) and [Definition of Done](docs/process/definition-of-done.md)
- [Role cards](docs/team/roles/)
- [Architecture decisions](docs/adr/)
