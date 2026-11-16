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

The document store tests also need Azurite, the local stand in for Azure Blob Storage. It needs no password:

```bash
docker run -d --name argus-azurite -p 10000:10000 mcr.microsoft.com/azure-storage/azurite:3.37.0 azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
ARGUS_TEST_BLOB_CONNECTION_STRING=UseDevelopmentStorage=true .venv/bin/pytest
```

To create the tables in a database, set `ARGUS_DATABASE_URL` and run `.venv/bin/alembic upgrade head`. The migrations are in `src/argus/db/migrations/`.

CI runs the same tests plus `ruff check`, `ruff format --check`, `mypy`, the fixture check and a secret scan on every PR. Tag each test with the requirements it covers, for example `@pytest.mark.req("IN-04", "IN-09")`. An ID that is not in the catalog fails the run.

### Acceptance tests and requirement coverage

The acceptance tests AC-1 to AC-8 (`tests/acceptance/`) run the fixture emails end to end through the stand ins. They need the same PostgreSQL and Azurite as above. One command runs them all and prints pass, fail or not run for each AC:

```bash
.venv/bin/python tools/acceptance.py
```

An AC passes only when every one of its checks ran and passed. A scenario whose feature is not built yet carries `@pytest.mark.waits_on(<issue>)` and shows as not run. `--markdown FILE` also appends the result as Markdown, for the sprint review. CI writes it to the job summary.

To list every requirement ID with the tests that cover it, or only the ones without a test:

```bash
.venv/bin/python tools/requirement_coverage.py
.venv/bin/python tools/requirement_coverage.py --missing
```

## Original documents

Every received email and each attachment is kept byte for byte in blob storage, named by its SHA-256 fingerprint (`src/argus/documents/`, ADR 0001). Storing the same file again finds the first copy and never replaces it, and every read checks the fingerprint again. The database records which file came from which email and which case it belongs to, and writes the fingerprint into the audit log itself. In Azure the connection string comes from Key Vault as `ARGUS_DOCUMENTS_CONNECTION_STRING`.

## Nightly copy of the audit log

The hash chain in the database catches a change to an old audit event, but not the newest events being deleted. So every night `python -m argus.audit` copies the new events into `audit-log/<YYYY-MM-DD>.jsonl` in the same locked container as the documents, then compares the database with every copy (`src/argus/audit/`, issue #83). It reports a copied event that is missing or changed, and any gap in the chain numbers. It also reports a copy for a night after the one being run, and events older than the newest copy's night that no copy holds, so copies written ahead of time cannot switch the copying off. A failure of either step opens an exception (`audit_copy_failed` or `audit_check_failed`). The same problem found again keeps its open item; a different problem gets its own. Copies cannot change, so a real problem, such as a deleted event, is found again every night and opens a new item after a person resolves the old one. The job only reads the database. It needs `ARGUS_DATABASE_URL` and `ARGUS_DOCUMENTS_CONNECTION_STRING`; `--night 2026-11-03` names the night, otherwise it is today in Berlin, and a night after today is refused. Running it twice for the same night writes nothing the second time. In Azure it runs as the scheduled job `caj-argus-audit-copy` every night at 01:00 UTC, without retries (#103). Every run ends with the line `argus-job ok audit-copy` or `argus-job failed audit-copy`, and an alert emails Arita on a failed run or when no run finished for 26 hours, because when the database cannot be reached the job cannot put anything in the exception queue.

## Stand ins

ARGUS cannot reach Wise or DATEV from the build environment, so development and CI use stand ins that answer like the real systems. See [standins/wise/README.md](standins/wise/README.md) for the Wise one. Sign in uses a fake Microsoft Entra ID locally and in CI, see [standins/entra/README.md](standins/entra/README.md).

## Start here

- [Team charter](CLAUDE.md): what we build, how the team works, the rules
- [Requirements catalog](docs/requirements/requirements.md)
- [Workflow, board and ceremonies](docs/process/workflow.md)
- [Definition of Ready](docs/process/definition-of-ready.md) and [Definition of Done](docs/process/definition-of-done.md)
- [Role cards](docs/team/roles/)
- [Architecture decisions](docs/adr/)
