# ARGUS

Bookkeeping automation for Max ASP GmbH: classify and route every email in the accounts inbox, book supplier invoices, outgoing invoices and Wise bank transactions in DATEV, bundle approved invoices into Wise payment runs twice a month, and keep a full audit trail from receipt to payment.

## Run the tests

You need Python 3.12 or newer. From the repository root:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

CI runs the same tests plus `ruff check`, `ruff format --check`, `mypy`, the fixture check and a secret scan on every PR. Tag each test with the requirements it covers, for example `@pytest.mark.req("IN-04", "IN-09")`. An ID that is not in the catalog fails the run.

## Start here

- [Team charter](CLAUDE.md): what we build, how the team works, the rules
- [Requirements catalog](docs/requirements/requirements.md)
- [Workflow, board and ceremonies](docs/process/workflow.md)
- [Definition of Ready](docs/process/definition-of-ready.md) and [Definition of Done](docs/process/definition-of-done.md)
- [Role cards](docs/team/roles/)
- [Architecture decisions](docs/adr/)
