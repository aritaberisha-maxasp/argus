"""The scheduled intake jobs in Azure: the mailbox sweep and the daily removal (#125, IN-01, NF-01).

ADR 0001 runs every scheduled job as a Container Apps job that starts, does one pass and stops.
The scheduler only says "it is time"; each run checks what was already done before it does
anything, so a run that starts twice or late does no harm.

- ``python -m argus.intake sweep``: one pass of :func:`argus.intake.sweep.sweep`. Azure starts
  it every 5 minutes. Only one sweep works at a time: a run that finds another one holding the
  sweep lock does nothing and counts as a clean run. Filing into the ARGUS folders happens at
  the end of every sweep (#26), so it needs no job of its own.
- ``python -m argus.intake removal``: one pass of :func:`argus.intake.filing.remove_due`, the
  30 day removal from ARGUS/Removed. Azure starts it once a day.
- ``python -m argus.intake digest``: :func:`argus.intake.digest.send_daily_digest`, the daily
  email to Benjamin with every open exception (EX-03, #61). Azure starts it once a day in the
  morning. It needs only the database and a Graph client that may send the digest and nothing
  else, so it does not build the sorting or the reading.

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
from typing import Final

from sqlalchemy import Engine

from argus.connections import connections_from_env
from argus.db import engine_from_env
from argus.db.mode import DatabaseModeError
from argus.db.mode import check_database_for_mode as _check_database_for_mode
from argus.documents.store import AzureDocumentStore
from argus.intake.digest import app_url_from_environment, send_daily_digest
from argus.intake.email_text import EmailReader
from argus.intake.filing import Folders, remove_due
from argus.intake.graph import GraphClient
from argus.intake.invoices import Invoicing
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
JOBS: Final = (SWEEP, REMOVAL, DIGEST)

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


class JobSettingsError(ValueError):
    """A setting the job needs is missing or malformed. The message names the setting, never
    its value."""


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


@dataclass
class Services:
    """What one run of a job works with. ``close`` gives back connections and clients."""

    engine: Engine
    graph: GraphClient
    archive: DocumentArchive
    sorting: Sorting
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


def check_database_for_mode(environ: Mapping[str, str], *, demo: bool) -> None:
    """Demo data only in the demo database, real data never in it (#124, ADR 0009, QA on PR
    130). The rule lives in :mod:`argus.db.mode`, shared with the nightly audit copy."""
    try:
        _check_database_for_mode(environ, demo=demo)
    except DatabaseModeError as error:
        raise JobSettingsError(str(error).replace("the job", "the intake jobs")) from None


def services_from_env(environ: Mapping[str, str]) -> Services:
    """Build everything from the settings Azure gives the job (infra/modules/apps.bicep).

    The mailbox comes from :mod:`argus.connections`, the documents from the document store
    secret (the demo store in demo mode), the sorting and the reading from Azure OpenAI and
    Document Intelligence in Max ASP's tenant (ADR 0006)."""
    settings = IntakeSettings.from_environment(environ)
    config = settings.sorting_config()
    secrets = EnvironmentSecretStore(environ)
    resources = ExitStack()
    try:
        connections = connections_from_env(environ)
        check_database_for_mode(environ, demo=connections.demo)
        graph = connections.graph_client()
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
        sorting = Sorting(
            documents=documents,
            model=model,
            config=config,
            read=EmailReader(reader),
            # Without the reminder address the client cannot forward, so reminders wait in the
            # Inbox until it is set, as the sweep has always done (#27).
            reminders=graph if graph.forward_to else None,
            invoicing=Invoicing(reader, settings.own_vat_ids, field_model),
        )
        engine = engine_from_env(environ=environ)
        resources.callback(engine.dispose)
    except BaseException:
        resources.close()
        raise
    return Services(engine, graph, DocumentArchive(documents), sorting, resources)


def digest_services_from_env(environ: Mapping[str, str]) -> DigestServices:
    """Build what the digest job needs: the database, a Graph client that may send the digest
    to ``ARGUS_DIGEST_TO`` and nothing else, and the app's address for the links (#61)."""
    app_url = app_url_from_environment(environ)
    resources = ExitStack()
    try:
        connections = connections_from_env(environ)
        check_database_for_mode(environ, demo=connections.demo)
        if not connections.digest_to:
            raise JobSettingsError("set ARGUS_DIGEST_TO before the digest job can run")
        graph = connections.graph_client(digest=True)
        resources.callback(graph.close)
        engine = engine_from_env(environ=environ)
        resources.callback(engine.dispose)
    except BaseException:
        resources.close()
        raise
    return DigestServices(engine, graph, app_url, resources)


def run_sweep(services: Services) -> bool:
    """One sweep. True when nothing failed, including when another sweep was running."""
    result = sweep(services.engine, services.graph, services.archive, services.sorting)
    return result.failed == 0


def run_removal(services: Services) -> bool:
    """One pass of the 30 day removal. True when nothing failed."""
    result = remove_due(services.engine, services.graph, Folders(services.graph))
    return result.failed == 0


def run_digest(services: DigestServices) -> bool:
    """Today's digest of open exceptions. True when it was sent, was sent already, was not
    needed or another run was sending it; False when the recorded draft was gone or changed."""
    return send_daily_digest(services.engine, services.graph, services.app_url).ok


RUNNERS: Final[dict[str, Callable[[Services], bool]]] = {SWEEP: run_sweep, REMOVAL: run_removal}


def _run(
    job: str,
    environ: Mapping[str, str],
    build: Callable[[Mapping[str, str]], Services],
    build_digest: Callable[[Mapping[str, str]], DigestServices],
) -> bool:
    if job == DIGEST:
        digest = build_digest(environ)
        try:
            return run_digest(digest)
        finally:
            digest.close()
    services = build(environ)
    try:
        return RUNNERS[job](services)
    finally:
        services.close()


def run_job(
    job: str,
    environ: Mapping[str, str],
    build: Callable[[Mapping[str, str]], Services] = services_from_env,
    build_digest: Callable[[Mapping[str, str]], DigestServices] = digest_services_from_env,
) -> bool:
    """Run one job once and log its outcome line. Never raises: a failure is logged through
    :mod:`argus.logsafe`, as its type and codes only."""
    ok = False
    try:
        ok = _run(job, environ, build, build_digest)
    except Exception:
        log.exception("The %s job could not finish", job)
    log_outcome(log, job, ok)
    return ok


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    build: Callable[[Mapping[str, str]], Services] = services_from_env,
    build_digest: Callable[[Mapping[str, str]], DigestServices] = digest_services_from_env,
) -> int:
    """Entry point: ``python -m argus.intake sweep``, ``removal`` or ``digest``."""
    parser = argparse.ArgumentParser(
        prog="python -m argus.intake",
        description=(
            "Run one scheduled intake job once: the mailbox sweep, the daily removal or the "
            "daily digest of open exceptions."
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
    return 0 if run_job(args.job, environ, build, build_digest) else 1
