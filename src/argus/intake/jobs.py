"""The scheduled intake jobs in Azure: the mailbox sweep and the daily removal (#125, IN-01, NF-01).

ADR 0001 runs every scheduled job as a Container Apps job that starts, does one pass and stops.
The scheduler only says "it is time"; each run checks what was already done before it does
anything, so a run that starts twice or late does no harm.

- ``python -m argus.intake sweep``: one pass of :func:`argus.intake.sweep.sweep`. Azure starts
  it every 5 minutes. Only one sweep works at a time: a run that finds another one holding the
  sweep lock does nothing and counts as a clean run. The sweep also sends each checked invoice
  to its approver (#191), with a second Graph client that may send approval requests only;
  without ``ARGUS_APP_URL`` for the link, checked invoices wait in Received. Filing into the
  ARGUS folders happens at the end of every sweep (#26), so it needs no job of its own.
- ``python -m argus.intake removal``: one pass of :func:`argus.intake.filing.remove_due`, the
  30 day removal from ARGUS/Removed. Azure starts it once a day.
- ``python -m argus.intake digest``: :func:`argus.intake.digest.send_daily_digest`, the daily
  email to Benjamin with every open exception (EX-03, #61). Azure starts it once a day in the
  morning. It needs only the database and a Graph client that may send the digest and nothing
  else, so it does not build the sorting or the reading.
- ``python -m argus.intake statements``: :func:`argus.bank.notices.remind_missing_statements`,
  the daily check for a missing Wise statement and the reminder to Benjamin (BA-01, #166).
  Azure starts it once a day in the morning. It needs only the database and the Graph client
  that may send the notice, and refuses to start without ``ARGUS_REMINDER_FORWARD_TO``. The
  sweep hands the statement emails to the import (:mod:`argus.intake.statements`).

Exit code 0 when the run went through clean, 1 when it failed as a whole (the database or the
mailbox could not be reached, a setting is missing) or any single email failed. A failed email
is tried again by the next run, as before. Every run ends with exactly one log line
``argus-job ok <job>`` or ``argus-job failed <job>``. The Azure alerts watch these lines
(``infra/modules/alerts.bicep``): a failed line, or no ok line for too long, emails Arita. The
lines name the job only, never an email, a sender or a subject (NF-03).

Where the mailbox is comes from :mod:`argus.connections`: the fake mailbox in demo mode, the
real one at go live. This module never looks at the mode itself.
"""

import argparse
import logging
import os
import re
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from typing import Final

import httpx
from sqlalchemy import Engine, text

from argus.bank.importer import BankSettings
from argus.bank.notices import remind_missing_statements
from argus.connections import (
    DIGEST_TO_ENV,
    Connections,
    ConnectionSettingsError,
    NotConnected,
    connections_from_env,
)
from argus.datev.datevconnect import DatevConnectGateway
from argus.db import engine_from_env
from argus.db.mode import DatabaseModeError
from argus.db.mode import check_database_for_mode as _check_database_for_mode
from argus.documents.store import AzureDocumentStore
from argus.intake.accounts import chart_from_env
from argus.intake.approvals import Approvals
from argus.intake.digest import (
    APP_URL_ENV,
    DigestSettingsError,
    app_url_from_environment,
    berlin_today,
    send_daily_digest,
)
from argus.intake.email_text import EmailReader
from argus.intake.filing import Folders, remove_due
from argus.intake.graph import GraphCall, GraphClient, lookup_name_ok
from argus.intake.invoices import Invoicing
from argus.intake.statements import Statements
from argus.intake.sweep import DocumentArchive, Sorting, sweep
from argus.logsafe import FAILED_MARK as FAILED_MARK
from argus.logsafe import OK_MARK as OK_MARK
from argus.logsafe import log_outcome, use_safe_logging
from argus.reading.document_intelligence import AzureDocumentIntelligence
from argus.reading.language_model import AzureOpenAIFieldModel
from argus.secrets import EnvironmentSecretStore
from argus.sorting import SortingConfig
from argus.sorting.config import Thresholds
from argus.sorting.language_model import AzureOpenAIModel

log = logging.getLogger("argus.intake.jobs")

SWEEP: Final = "sweep"
REMOVAL: Final = "removal"
DIGEST: Final = "digest"
STATEMENTS: Final = "statements"
JOBS: Final = (SWEEP, REMOVAL, DIGEST, STATEMENTS)

# Who Max ASP is, for the sorting (#25, #70) and the invoice reading (#30, #106). Plain values,
# not secrets. In demo mode the templates set the synthetic values of the test fixtures.
OWN_DOMAIN_ENV: Final = "ARGUS_OWN_DOMAIN"
INVOICING_AGENT_ENV: Final = "ARGUS_INVOICING_AGENT"
OWN_VAT_IDS_ENV: Final = "ARGUS_OWN_VAT_IDS"
VENDOR_DOMAINS_ENV: Final = "ARGUS_VENDOR_DOMAINS"
CUSTOMER_DOMAINS_ENV: Final = "ARGUS_CUSTOMER_DOMAINS"
REQUIRED_SETTINGS: Final = (OWN_DOMAIN_ENV, INVOICING_AGENT_ENV, OWN_VAT_IDS_ENV)
# Azure OpenAI and Document Intelligence, set by infra/modules/apps.bicep.
OPENAI_ENDPOINT_ENV: Final = "AZURE_OPENAI_ENDPOINT"
OPENAI_DEPLOYMENT_ENV: Final = "AZURE_OPENAI_DEPLOYMENT"
DOCUMENT_INTELLIGENCE_ENDPOINT_ENV: Final = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"

_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_VAT_ID = re.compile(r"[A-Z]{2}[A-Z0-9]{2,12}")
# One email address, nothing around it: no list, no display name (#167).
_ADDRESS = re.compile(r"[^@\s,;<>]+@" + _DOMAIN.pattern, re.IGNORECASE)


class JobSettingsError(ValueError):
    """A setting the job needs is missing or malformed. The message names the setting, never
    its value."""


# Errors whose message names a setting and never its value. A job that stops on one logs the
# message, so the log says which setting to fix (#167). Every other error is logged as its type
# and the place it was raised only (NF-03).
SETTINGS_ERRORS: Final = (
    JobSettingsError,
    DigestSettingsError,
    ConnectionSettingsError,
    NotConnected,
)


def _items(environ: Mapping[str, str], name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in environ.get(name, "").split(",") if item.strip())


def _domains(environ: Mapping[str, str], name: str) -> frozenset[str]:
    found = frozenset(item.lower() for item in _items(environ, name))
    if any(not _DOMAIN.fullmatch(domain) for domain in found):
        raise JobSettingsError(f"{name} must be a comma separated list of email domains")
    return found


@dataclass(frozen=True)
class IntakeSettings:
    """Who Max ASP is and who its partners are, from the environment."""

    own_domain: str
    invoicing_agent: str
    own_vat_ids: frozenset[str]
    vendor_domains: frozenset[str] = frozenset()
    customer_domains: frozenset[str] = frozenset()
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "IntakeSettings":
        missing = [name for name in REQUIRED_SETTINGS if not environ.get(name, "").strip()]
        if missing:
            raise JobSettingsError(f"set {', '.join(missing)} before the intake jobs can run")
        own_domain = environ[OWN_DOMAIN_ENV].strip().lower()
        if not _DOMAIN.fullmatch(own_domain):
            raise JobSettingsError(f"{OWN_DOMAIN_ENV} must be one email domain")
        own_vat_ids = frozenset(
            re.sub(r"\s", "", item).upper() for item in _items(environ, OWN_VAT_IDS_ENV)
        )
        if any(not _VAT_ID.fullmatch(vat_id) for vat_id in own_vat_ids):
            raise JobSettingsError(f"{OWN_VAT_IDS_ENV} must be a comma separated list of VAT IDs")
        return cls(
            own_domain=own_domain,
            invoicing_agent=environ[INVOICING_AGENT_ENV].strip().lower(),
            own_vat_ids=own_vat_ids,
            vendor_domains=_domains(environ, VENDOR_DOMAINS_ENV),
            customer_domains=_domains(environ, CUSTOMER_DOMAINS_ENV),
            thresholds=Thresholds.from_environment(environ),
        )

    def sorting_config(self) -> SortingConfig:
        try:
            return SortingConfig(
                own_domain=self.own_domain,
                invoicing_agent=self.invoicing_agent,
                vendor_domains=self.vendor_domains,
                customer_domains=self.customer_domains,
                own_vat_ids=self.own_vat_ids,
                thresholds=self.thresholds,
            )
        except ValueError:
            # The message would not quote the value, but name the setting to be sure.
            raise JobSettingsError(f"{INVOICING_AGENT_ENV} must be one email address") from None


# The Graph calls each job may make (#168, NF-03). A call that is not on a job's list is
# refused before it leaves ARGUS, so a bug in one job cannot touch mail it has no business
# with. Add a call here only together with the code that needs it, and say why.

# The sweep itself (sweep.py, filing.py, shadow.py): list the Inbox and read each email, find
# or create the ARGUS folders and file the email there, and in shadow mode see which folder a
# person moved an email to. It never deletes.
SWEEP_CALLS: Final = frozenset(
    {
        GraphCall.LIST_INBOX,
        GraphCall.READ_EMAIL,
        GraphCall.FIND_FOLDERS,
        GraphCall.CREATE_FOLDER,
        GraphCall.MOVE,
        GraphCall.READ_MESSAGE,
        GraphCall.READ_INBOX_ID,
        GraphCall.READ_DELETED_ITEMS_ID,
    }
)
# Reminder forward, run by the sweep (reminders.py, #27): save a forward as a draft, read it
# back and send it.
REMINDER_FORWARD_CALLS: Final = frozenset(
    {GraphCall.FORWARD, GraphCall.READ_MESSAGE, GraphCall.SEND_DRAFT}
)
# Statement import, run by the sweep (sweep.py, #166): find or create the statement folder,
# list it, read and move its emails, and send a notice about a blocked statement (#56).
STATEMENT_IMPORT_CALLS: Final = frozenset(
    {
        GraphCall.FIND_FOLDERS,
        GraphCall.CREATE_FOLDER,
        GraphCall.LIST_STATEMENT_FOLDER,
        GraphCall.READ_EMAIL,
        GraphCall.MOVE,
        GraphCall.SEND_NOTICE,
    }
)
# The sweep job runs those three with one client.
SWEEP_JOB_CALLS: Final = SWEEP_CALLS | REMINDER_FORWARD_CALLS | STATEMENT_IMPORT_CALLS
# The 30 day removal (filing.remove_due, #26): find ARGUS/Removed, check which folder an
# email is in and delete it. It never moves, lists the Inbox or sends.
REMOVAL_CALLS: Final = frozenset(
    {
        GraphCall.FIND_FOLDERS,
        GraphCall.READ_MESSAGE,
        GraphCall.READ_DELETED_ITEMS_ID,
        GraphCall.DELETE,
    }
)
# The daily digest (digest.py, #61): save the digest as a draft, read it back and send it.
DIGEST_CALLS: Final = frozenset(
    {GraphCall.CREATE_DIGEST, GraphCall.READ_MESSAGE, GraphCall.SEND_DRAFT}
)
# The statement reminder (bank/notices.py, #166): send a notice about a missing statement.
STATEMENT_REMINDER_CALLS: Final = frozenset({GraphCall.SEND_NOTICE})
# Approval requests, run by the sweep (approvals.py, #191): email a checked invoice to the
# person on its stored approval request. A client of its own, with this call only, so no other
# step of the sweep can send one, and it can send nothing else.
APPROVAL_REQUEST_CALLS: Final = frozenset({GraphCall.SEND_APPROVAL_REQUEST})


@dataclass
class Services:
    """What one run of a job works with. ``close`` gives back connections and clients."""

    engine: Engine
    graph: GraphClient
    archive: DocumentArchive
    sorting: Sorting
    _resources: ExitStack = field(default_factory=ExitStack)
    # Hands statement emails to the Wise statement import (#166).
    statements: Statements | None = None

    def close(self) -> None:
        self._resources.close()


@dataclass
class StatementServices:
    """What one run of the statement job works with (#166): the database, the Wise balances
    to look for and the Graph client that may send the notice."""

    engine: Engine
    graph: GraphClient
    settings: BankSettings
    _resources: ExitStack = field(default_factory=ExitStack)

    def close(self) -> None:
        self._resources.close()


@dataclass
class DigestServices:
    """What one run of the digest job works with (#61): no archive, no sorting, only the app's
    address for the links."""

    engine: Engine
    graph: GraphClient
    app_url: str
    _resources: ExitStack = field(default_factory=ExitStack)

    def close(self) -> None:
        self._resources.close()


def _setting(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise JobSettingsError(f"set {name} before the intake jobs can run")
    return value


def bank_settings(environ: Mapping[str, str]) -> BankSettings:
    """Who may send Wise statements, the statement folder and the balances (#56, #166)."""
    try:
        settings = BankSettings.from_environment(environ)
    except ValueError as error:
        raise JobSettingsError(str(error)) from None
    if not all(lookup_name_ok(name) for name in settings.statement_folder.strip("/").split("/")):
        raise JobSettingsError(
            "ARGUS_STATEMENT_FOLDER must be a folder path such as ARGUS/Kontoauszüge,"
            " with letters and spaces only"
        )
    return settings


def check_database_for_mode(environ: Mapping[str, str], *, demo: bool) -> None:
    """Demo data only in the demo database, real data never in it (#124, ADR 0009, QA on PR
    130). The rule lives in :mod:`argus.db.mode`, shared with the nightly audit copy."""
    try:
        _check_database_for_mode(environ, demo=demo)
    except DatabaseModeError as error:
        raise JobSettingsError(str(error).replace("the job", "the intake jobs")) from None


def services_from_env(
    environ: Mapping[str, str], calls: frozenset[GraphCall] = SWEEP_JOB_CALLS
) -> Services:
    """Build everything from the settings Azure gives the job (infra/modules/apps.bicep).
    ``calls`` are the Graph calls the job may make: the sweep's unless named (#168).

    The mailbox comes from :mod:`argus.connections`, the documents from the document store
    secret (the demo store in demo mode), the sorting and the reading from Azure OpenAI and
    Document Intelligence in Max ASP's tenant (ADR 0006)."""
    settings = IntakeSettings.from_environment(environ)
    config = settings.sorting_config()
    bank = bank_settings(environ)
    secrets = EnvironmentSecretStore(environ)
    resources = ExitStack()
    try:
        connections = connections_from_env(environ)
        check_database_for_mode(environ, demo=connections.demo)
        graph = connections.graph_client(statement_folder=bank.statement_folder, calls=calls)
        resources.callback(graph.close)
        documents = AzureDocumentStore.from_secrets(secrets)
        openai_endpoint = _setting(environ, OPENAI_ENDPOINT_ENV)
        deployment = _setting(environ, OPENAI_DEPLOYMENT_ENV)
        reader = AzureDocumentIntelligence.from_secret_store(
            secrets, _setting(environ, DOCUMENT_INTELLIGENCE_ENDPOINT_ENV)
        )
        resources.callback(reader.close)
        model = AzureOpenAIModel.from_secret_store(secrets, openai_endpoint, deployment)
        resources.callback(model.close)
        field_model = AzureOpenAIFieldModel.from_secret_store(secrets, openai_endpoint, deployment)
        resources.callback(field_model.close)
        datev = _datev_gateway(connections)
        if datev is not None:
            resources.callback(datev[1].close)
        approvals = None
        if calls == SWEEP_JOB_CALLS:
            approvals = _approvals(environ, connections, resources)
        sorting = Sorting(
            documents=documents,
            model=model,
            config=config,
            read=EmailReader(reader),
            # Without the reminder address the client cannot forward, so reminders wait in the
            # Inbox until it is set, as the sweep has always done (#27).
            reminders=graph if graph.forward_to else None,
            invoicing=Invoicing(reader, settings.own_vat_ids, field_model),
            # Without DATEV, cases wait without a vendor account until it is set (#35).
            vendors=datev[0] if datev is not None else None,
            chart=chart_from_env(environ),
            approvals=approvals,
        )
        # A notice about a blocked statement goes out right away when the reminder address
        # is set; otherwise the daily statement job sends it (#166).
        statements = Statements(documents, bank, graph if graph.forward_to else None)
        engine = engine_from_env(environ=environ)
        resources.callback(engine.dispose)
    except BaseException:
        resources.close()
        raise
    return Services(
        engine, graph, DocumentArchive(documents), sorting, resources, statements=statements
    )


def removal_services_from_env(environ: Mapping[str, str]) -> Services:
    """Build what the 30 day removal needs. Its Graph client may only find ARGUS/Removed,
    check where an email is and delete it (#168)."""
    return services_from_env(environ, calls=REMOVAL_CALLS)


def _approvals(
    environ: Mapping[str, str], connections: Connections, resources: ExitStack
) -> Approvals | None:
    """The approval request client and the app's address for the link (#191), or None while
    ``ARGUS_APP_URL`` is not set: checked invoices then wait in Received."""
    if not environ.get(APP_URL_ENV, "").strip():
        log.info("ARGUS_APP_URL is not set, so checked invoices wait in Received")
        return None
    app_url = app_url_from_environment(environ)
    client = connections.graph_client(calls=APPROVAL_REQUEST_CALLS)
    resources.callback(client.close)
    return Approvals(client, app_url)


def _datev_gateway(connections: Connections) -> tuple[DatevConnectGateway, httpx.Client] | None:
    """The DATEV gateway and its HTTP client, or None while DATEV is not set (#35). The demo
    sets the fake DATEV; live DATEV is set at go live (docs/runbooks/go-live.md)."""
    if not connections.datev_url or not connections.datev_client:
        log.info("DATEV is not set, so cases are not matched to vendor accounts")
        return None
    http = httpx.Client(base_url=connections.datev_url, timeout=30.0)
    return DatevConnectGateway(http, connections.datev_client), http


def statement_services_from_env(environ: Mapping[str, str]) -> StatementServices:
    """Build what the statement job needs: the database, the Wise balances and a Graph client
    that may send the notice to ``ARGUS_REMINDER_FORWARD_TO`` (#166)."""
    settings = bank_settings(environ)
    resources = ExitStack()
    try:
        connections = connections_from_env(environ)
        check_database_for_mode(environ, demo=connections.demo)
        if not connections.reminder_forward_to:
            raise JobSettingsError(
                "set ARGUS_REMINDER_FORWARD_TO before the statements job can run"
            )
        graph = connections.graph_client(calls=STATEMENT_REMINDER_CALLS)
        resources.callback(graph.close)
        engine = engine_from_env(environ=environ)
        resources.callback(engine.dispose)
    except BaseException:
        resources.close()
        raise
    return StatementServices(engine, graph, settings, resources)


def digest_services_from_env(environ: Mapping[str, str]) -> DigestServices:
    """Build what the digest job needs: the database, a Graph client that may send the digest
    to ``ARGUS_DIGEST_TO`` and nothing else, and the app's address for the links (#61)."""
    app_url = app_url_from_environment(environ)
    resources = ExitStack()
    try:
        connections = connections_from_env(environ)
        check_database_for_mode(environ, demo=connections.demo)
        if not connections.digest_to:
            raise JobSettingsError(
                f"set {DIGEST_TO_ENV} before the digest job can run; at go live it is the "
                "GitHub secret of the same name (docs/runbooks/go-live.md)"
            )
        if not _ADDRESS.fullmatch(connections.digest_to):
            raise JobSettingsError(f"{DIGEST_TO_ENV} must be exactly one email address")
        graph = connections.graph_client(digest=True, calls=DIGEST_CALLS)
        resources.callback(graph.close)
        engine = engine_from_env(environ=environ)
        resources.callback(engine.dispose)
    except BaseException:
        resources.close()
        raise
    return DigestServices(engine, graph, app_url, resources)


def run_sweep(services: Services) -> bool:
    """One sweep. True when nothing failed, including when another sweep was running."""
    result = sweep(
        services.engine, services.graph, services.archive, services.sorting, services.statements
    )
    return result.failed == 0


def run_removal(services: Services) -> bool:
    """One pass of the 30 day removal. True when nothing failed."""
    result = remove_due(services.engine, services.graph, Folders(services.graph))
    return result.failed == 0


def run_digest(services: DigestServices) -> bool:
    """Today's digest of open exceptions. True when it was sent, was sent already, was not
    needed or another run was sending it; False when the recorded draft was gone or changed."""
    return send_daily_digest(services.engine, services.graph, services.app_url).ok


def run_statements(services: StatementServices, today: date | None = None) -> bool:
    """Today's check for a missing Wise statement (BA-01, #166). True when every notice that
    was due went out, or was recorded as not sent in shadow mode; False when one is still
    waiting because sending failed. The next run tries it again."""
    remind_missing_statements(
        services.engine,
        services.settings.balances,
        services.graph,
        today=today or berlin_today(),
    )
    with services.engine.connect() as connection:
        waiting: int = connection.execute(
            text("select count(*) from bank_notice where status = 'pending'")
        ).scalar_one()
    return waiting == 0


RUNNERS: Final[dict[str, Callable[[Services], bool]]] = {SWEEP: run_sweep, REMOVAL: run_removal}
# Each job builds its own services, so each gets a Graph client with its own calls (#168).
BUILDERS: Final[dict[str, Callable[[Mapping[str, str]], Services]]] = {
    SWEEP: services_from_env,
    REMOVAL: removal_services_from_env,
}


def _run(
    job: str,
    environ: Mapping[str, str],
    build: Callable[[Mapping[str, str]], Services] | None,
    build_digest: Callable[[Mapping[str, str]], DigestServices],
    build_statements: Callable[[Mapping[str, str]], StatementServices],
) -> bool:
    if job == STATEMENTS:
        statements = build_statements(environ)
        try:
            return run_statements(statements)
        finally:
            statements.close()
    if job == DIGEST:
        digest = build_digest(environ)
        try:
            return run_digest(digest)
        finally:
            digest.close()
    services = (build or BUILDERS[job])(environ)
    try:
        return RUNNERS[job](services)
    finally:
        services.close()


def run_job(
    job: str,
    environ: Mapping[str, str],
    build: Callable[[Mapping[str, str]], Services] | None = None,
    build_digest: Callable[[Mapping[str, str]], DigestServices] = digest_services_from_env,
    build_statements: Callable[
        [Mapping[str, str]], StatementServices
    ] = statement_services_from_env,
) -> bool:
    """Run one job once and log its outcome line. ``build`` replaces the job's own builder in
    tests. Never raises: a failure is logged through
    :mod:`argus.logsafe`, as its type and codes only."""
    ok = False
    try:
        ok = _run(job, environ, build, build_digest, build_statements)
    except SETTINGS_ERRORS as error:
        # These name the missing or wrong setting and never its value, so the reason can go
        # into the log as it is. The safe formatter would only write the error's type, and
        # Arita needs to see which GitHub secret to set (#167).
        log.error("The %s job cannot run: %s", job, error)
    except Exception:
        log.exception("The %s job could not finish", job)
    log_outcome(log, job, ok)
    return ok


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    build: Callable[[Mapping[str, str]], Services] | None = None,
    build_digest: Callable[[Mapping[str, str]], DigestServices] = digest_services_from_env,
    build_statements: Callable[
        [Mapping[str, str]], StatementServices
    ] = statement_services_from_env,
) -> int:
    """Entry point: ``python -m argus.intake sweep``, ``removal``, ``digest`` or
    ``statements``."""
    parser = argparse.ArgumentParser(
        prog="python -m argus.intake",
        description=(
            "Run one scheduled intake job once: the mailbox sweep, the daily removal, the "
            "daily digest of open exceptions or the daily check for a missing Wise statement."
        ),
    )
    parser.add_argument("job", choices=JOBS)
    args = parser.parse_args(argv)
    # Errors as type and codes only, never their text: a database error can quote the row,
    # with a sender or a subject in it (NF-03, review of PR 131).
    use_safe_logging()
    # One line per HTTP call every 5 minutes would fill the logs; failures still show.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    environ = os.environ if environ is None else environ
    return 0 if run_job(args.job, environ, build, build_digest, build_statements) else 1
