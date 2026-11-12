"""The demo of ARGUS against the stand ins, for showing it to Max ASP (#124, ADR 0009).

``python -m argus.demo seed`` puts a fixed set of synthetic emails into the fake mailbox.
``python -m argus.demo reset`` empties the demo data so the demo can start over. Both refuse to
run unless demo mode is on (``ARGUS_DEMO=true``, see :mod:`argus.connections`), and they only
ever touch what belongs to the demo:

- the fake mailbox, the fake DATEV and the Wise stand in, which keep everything in memory;
- the demo database, whose name must end in ``_demo``;
- the demo document store, a storage account of its own without the 10 year lock, whose name
  must contain ``demo``. The real document store is locked and nothing could empty it anyway.

Every email comes from the synthetic fixtures in ``tests/fixtures/emails``. No real data.
"""

from argus.demo.common import DemoRefused, control_root
from argus.demo.data import DEMO_EMAILS, DemoEmail
from argus.demo.reset import (
    check_demo_database,
    check_demo_documents,
    database_name,
    documents_account,
    reset_database,
    reset_documents,
    reset_standins,
)
from argus.demo.seed import SeededEmail, seed

__all__ = [
    "DEMO_EMAILS",
    "DemoEmail",
    "DemoRefused",
    "SeededEmail",
    "check_demo_database",
    "check_demo_documents",
    "control_root",
    "database_name",
    "documents_account",
    "reset_database",
    "reset_documents",
    "reset_standins",
    "seed",
]
