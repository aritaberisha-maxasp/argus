"""The scheduled intake jobs: settings, outcome lines and exit codes (#125, IN-01, NF-01).

The sweep and the removal themselves are tested in tests/integration. Here the job around them
is tested: what it reads from the environment, that it never raises, and that every run ends
with the one line the Azure alerts look for.
"""

import logging
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from argus.connections import Connections, ConnectionSettingsError, NotConnected
from argus.datev import DatevConnectGateway
from argus.intake import jobs
from argus.intake.jobs import (
    FAILED_MARK,
    OK_MARK,
    IntakeSettings,
    JobSettingsError,
    Services,
    digest_services_from_env,
    main,
    run_job,
    services_from_env,
    statement_services_from_env,
)
from argus.logsafe import SafeFormatter
from standins.graph.app import fake_token

SETTINGS = {
    "ARGUS_OWN_DOMAIN": "maxasp.example",
    "ARGUS_INVOICING_AGENT": "rechnungen@maxasp.example",
    "ARGUS_OWN_VAT_IDS": "DE099000001",
    "ARGUS_VENDOR_DOMAINS": "muster-buerobedarf.example, beispiel-cloud.example",
    "ARGUS_CUSTOMER_DOMAINS": "beispiel-handel.example",
}
DEMO = {
    **SETTINGS,
    "ARGUS_DEMO": "true",
    "ARGUS_GRAPH_URL": "http://ca-argus-standin-graph/v1.0",
    "ARGUS_MAILBOX": "accounts@maxasp.example",
    "ARGUS_REMINDER_FORWARD_TO": "reminders@maxasp.example",
    "ARGUS_GRAPH_STANDIN_TOKEN": fake_token(["Mail.ReadWrite", "Mail.Send"]),
    "ARGUS_DATABASE_URL": "postgresql+psycopg://argus_app@localhost:5432/argus_demo",
    "ARGUS_DOCUMENTS_CONNECTION_STRING": "UseDevelopmentStorage=true",
    "AZURE_OPENAI_ENDPOINT": "https://openai.example",
    "AZURE_OPENAI_DEPLOYMENT": "argus-sorting",
    "AZURE_OPENAI_API_KEY": "not-a-real-key",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://reader.example",
    "AZURE_DOCUMENT_INTELLIGENCE_API_KEY": "not-a-real-key",
}


class FakeServices:
    """Stands in for :class:`Services`; counts closes."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def builder(services: Any) -> Any:
    def build(environ: Mapping[str, str]) -> Services:
        if isinstance(services, BaseException):
            raise services
        result: Services = services
        return result

    return build


def outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("argus-job ")]


# Settings


@pytest.mark.req("IN-01")
def test_settings_are_read_and_cleaned() -> None:
    settings = IntakeSettings.from_environment(
        {
            **SETTINGS,
            "ARGUS_OWN_DOMAIN": " MaxASP.example ",
            "ARGUS_OWN_VAT_IDS": "de 099 000 001, ATU12345678",
            "ARGUS_VENDOR_DOMAINS": "Muster-Buerobedarf.example,,",
        }
    )
    assert settings.own_domain == "maxasp.example"
    assert settings.own_vat_ids == frozenset({"DE099000001", "ATU12345678"})
    assert settings.vendor_domains == frozenset({"muster-buerobedarf.example"})
    assert settings.customer_domains == frozenset({"beispiel-handel.example"})
    config = settings.sorting_config()
    assert config.own_domain == "maxasp.example"
    assert config.invoicing_agent == "rechnungen@maxasp.example"


@pytest.mark.req("IN-01")
def test_partner_lists_may_be_empty() -> None:
    settings = IntakeSettings.from_environment(
        {k: v for k, v in SETTINGS.items() if "DOMAINS" not in k}
    )
    assert settings.vendor_domains == settings.customer_domains == frozenset()


@pytest.mark.req("IN-01")
@pytest.mark.parametrize("name", ["ARGUS_OWN_DOMAIN", "ARGUS_INVOICING_AGENT", "ARGUS_OWN_VAT_IDS"])
def test_a_missing_setting_is_named(name: str) -> None:
    with pytest.raises(JobSettingsError, match=name):
        IntakeSettings.from_environment({**SETTINGS, name: "  "})


@pytest.mark.req("IN-01")
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ARGUS_OWN_DOMAIN", "secret-value@maxasp.example"),
        ("ARGUS_OWN_VAT_IDS", "secret-value"),
        ("ARGUS_VENDOR_DOMAINS", "ok.example, secret value"),
        ("ARGUS_CUSTOMER_DOMAINS", "https://secret-value.example"),
        ("ARGUS_SORTING_THRESHOLD_OTHER", "secret-value"),
    ],
)
def test_a_malformed_setting_is_named_without_its_value(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name) as raised:
        IntakeSettings.from_environment({**SETTINGS, name: value})
    assert "secret" not in str(raised.value)


@pytest.mark.req("IN-01")
def test_an_invoicing_agent_without_at_is_named() -> None:
    settings = IntakeSettings.from_environment({**SETTINGS, "ARGUS_INVOICING_AGENT": "nobody"})
    with pytest.raises(JobSettingsError, match="ARGUS_INVOICING_AGENT"):
        settings.sorting_config()


# Building the services from what Azure gives the job


@pytest.mark.req("IN-01", "NF-06")
def test_demo_settings_build_the_job_without_calling_anything() -> None:
    services = services_from_env(DEMO)
    try:
        assert services.graph.mailbox == "accounts@maxasp.example"
        assert services.sorting.reminders is services.graph
        assert services.sorting.invoicing is not None
        assert services.sorting.invoicing.own_vat_ids == frozenset({"DE099000001"})
        assert services.sorting.config.own_vat_ids == frozenset({"DE099000001"})
        assert services.sorting.config.vendor_domains == frozenset(
            {"muster-buerobedarf.example", "beispiel-cloud.example"}
        )
        assert services.engine.url.database == "argus_demo"
    finally:
        services.close()


@pytest.mark.req("IN-01")
def test_without_a_reminder_address_reminders_wait() -> None:
    services = services_from_env({**DEMO, "ARGUS_REMINDER_FORWARD_TO": ""})
    try:
        assert services.sorting.reminders is None
    finally:
        services.close()


@pytest.mark.req("PR-02", "VM-03")
def test_with_datev_set_the_sweep_matches_vendors() -> None:
    services = services_from_env(
        {**DEMO, "ARGUS_DATEV_URL": "http://ca-argus-standin-datev", "ARGUS_DATEV_CLIENT": "x"}
    )
    try:
        assert isinstance(services.sorting.vendors, DatevConnectGateway)
    finally:
        services.close()


@pytest.mark.req("PR-02")
def test_without_datev_cases_wait_without_a_vendor_account() -> None:
    services = services_from_env({**DEMO, "ARGUS_DATEV_URL": "", "ARGUS_DATEV_CLIENT": ""})
    try:
        assert services.sorting.vendors is None
    finally:
        services.close()


@pytest.mark.req("IN-01", "NF-06")
@pytest.mark.parametrize(
    "name",
    [
        "ARGUS_DOCUMENTS_CONNECTION_STRING",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_DOCUMENT_INTELLIGENCE_API_KEY",
        "ARGUS_DATABASE_URL",
    ],
)
def test_a_missing_connection_stops_the_job_and_names_it(name: str) -> None:
    with pytest.raises(Exception, match=name):
        services_from_env({**DEMO, name: ""})


@pytest.mark.req("IN-01", "NF-06")
def test_the_job_keeps_to_the_demo_rule() -> None:
    with pytest.raises(ConnectionSettingsError):
        services_from_env({**DEMO, "ARGUS_GRAPH_URL": "https://graph.microsoft.com/v1.0"})


@pytest.mark.req("IN-01")
def test_live_without_a_microsoft_365_sign_in_does_not_run() -> None:
    live = {
        **{k: v for k, v in DEMO.items() if "STANDIN" not in k},
        "ARGUS_DEMO": "false",
        "ARGUS_GRAPH_URL": "https://graph.microsoft.com/v1.0",
        "ARGUS_DATABASE_URL": "postgresql+psycopg://argus_app@localhost:5432/argus",
    }
    with pytest.raises(NotConnected):
        services_from_env(live)


@pytest.mark.req("IN-01")
def test_clients_already_open_are_closed_when_building_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Graph:
        forward_to = None

        def close(self) -> None:
            closed.append("graph")

    class Connections:
        demo = True

        def graph_client(self, *, statement_folder: str | None = None, **_: Any) -> Graph:
            return Graph()

    monkeypatch.setattr(jobs, "connections_from_env", lambda environ: Connections())
    with pytest.raises(JobSettingsError, match="AZURE_OPENAI_ENDPOINT"):
        services_from_env({**DEMO, "AZURE_OPENAI_ENDPOINT": ""})
    assert closed == ["graph"]


# One outcome line per run, exit codes


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize("job", ["sweep", "removal"])
def test_a_clean_run_logs_ok_and_exits_0(
    job: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    services = FakeServices()
    monkeypatch.setitem(jobs.RUNNERS, job, lambda s: True)
    with caplog.at_level(logging.INFO):
        assert main([job], environ={}, build=builder(services)) == 0
    assert outcome_lines(caplog) == [f"{OK_MARK} {job}"]
    assert services.closed == 1


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize("job", ["sweep", "removal"])
def test_a_run_with_a_failed_email_logs_failed_and_exits_1(
    job: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    services = FakeServices()
    monkeypatch.setitem(jobs.RUNNERS, job, lambda s: False)
    with caplog.at_level(logging.INFO):
        assert main([job], environ={}, build=builder(services)) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} {job}"]
    assert services.closed == 1


@pytest.mark.req("IN-01", "NF-01")
def test_a_run_that_breaks_off_logs_failed_and_still_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    services = FakeServices()

    def down(s: Any) -> bool:
        raise ConnectionError("the database could not be reached")

    monkeypatch.setitem(jobs.RUNNERS, "sweep", down)
    with caplog.at_level(logging.INFO):
        assert main(["sweep"], environ={}, build=builder(services)) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} sweep"]
    assert services.closed == 1


@pytest.mark.req("IN-01", "NF-01")
def test_a_missing_setting_fails_the_run_and_names_it(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        assert main(["sweep"], environ={}) == 1
    assert outcome_lines(caplog) == [f"{FAILED_MARK} sweep"]
    assert "ARGUS_OWN_DOMAIN" in caplog.text


@pytest.mark.req("IN-01", "NF-03")
def test_the_log_holds_no_secret_or_token(caplog: pytest.LogCaptureFixture) -> None:
    secret = SecretStr(DEMO["ARGUS_GRAPH_STANDIN_TOKEN"])
    with caplog.at_level(logging.DEBUG):
        run_job("sweep", {**DEMO, "AZURE_OPENAI_ENDPOINT": ""})
    assert secret.get_secret_value() not in caplog.text
    assert "not-a-real-key" not in caplog.text


@pytest.mark.req("IN-01")
def test_only_the_two_jobs_exist() -> None:
    with pytest.raises(SystemExit):
        main(["payment-run"], environ={})


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize(
    ("failed", "skipped", "ok"), [(0, False, True), (1, False, False), (0, True, True)]
)
def test_a_sweep_with_a_failed_email_is_a_failed_run(
    monkeypatch: pytest.MonkeyPatch, failed: int, skipped: bool, ok: bool
) -> None:
    from argus.intake.sweep import SweepResult

    monkeypatch.setattr(
        jobs, "sweep", lambda *args: SweepResult(ran=not skipped, listed=2, failed=failed)
    )

    class Run:
        engine = graph = archive = sorting = statements = None

    assert jobs.run_sweep(Run()) is ok  # type: ignore[arg-type]


@pytest.mark.req("IN-01", "NF-01")
@pytest.mark.parametrize(("failed", "ok"), [(0, True), (1, False)])
def test_a_removal_with_a_failed_email_is_a_failed_run(
    monkeypatch: pytest.MonkeyPatch, failed: int, ok: bool
) -> None:
    from argus.intake.filing import RemovalResult

    class Run:
        engine = graph = None

    monkeypatch.setattr(jobs, "Folders", lambda graph: None)
    monkeypatch.setattr(jobs, "remove_due", lambda *args: RemovalResult(due=1, failed=failed))
    assert jobs.run_removal(Run()) is ok  # type: ignore[arg-type]


@pytest.mark.req("IN-01", "NF-06")
@pytest.mark.parametrize("database", ["argus", "argus_demo_copy", "argus_test", ""])
def test_demo_mode_refuses_a_database_not_named_demo(database: str) -> None:
    url = f"postgresql+psycopg://argus_app@localhost:5432/{database}"
    with pytest.raises(JobSettingsError, match="does not end in _demo") as raised:
        services_from_env({**DEMO, "ARGUS_DATABASE_URL": url})
    assert "argus_app" not in str(raised.value)


@pytest.mark.req("IN-01", "NF-06")
def test_live_mode_refuses_the_demo_database() -> None:
    live = {
        **{k: v for k, v in DEMO.items() if "STANDIN" not in k},
        "ARGUS_DEMO": "false",
        "ARGUS_GRAPH_URL": "https://graph.microsoft.com/v1.0",
    }
    with pytest.raises(JobSettingsError, match="demo mode is off but the database"):
        services_from_env(live)


@pytest.mark.req("IN-01")
def test_a_database_setting_that_is_no_url_is_named_without_its_value() -> None:
    with pytest.raises(JobSettingsError, match="ARGUS_DATABASE_URL") as raised:
        services_from_env({**DEMO, "ARGUS_DATABASE_URL": "secret-value"})
    assert "secret" not in str(raised.value)


@pytest.mark.req("IN-01")
def test_the_database_is_checked_before_any_client_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    class Connections:
        demo = True

        def graph_client(self) -> None:
            built.append("graph")

    monkeypatch.setattr(jobs, "connections_from_env", lambda environ: Connections())
    with pytest.raises(JobSettingsError):
        services_from_env({**DEMO, "ARGUS_DATABASE_URL": "postgresql+psycopg://x@h/argus"})
    assert built == []


@pytest.mark.req("BA-01", "BA-08")
def test_the_statement_job_needs_the_reminder_address() -> None:
    environ = {k: v for k, v in DEMO.items() if k != "ARGUS_REMINDER_FORWARD_TO"}
    with pytest.raises(JobSettingsError, match="ARGUS_REMINDER_FORWARD_TO"):
        statement_services_from_env(environ)


@pytest.mark.req("BA-01")
@pytest.mark.parametrize("folder", ["ARGUS/Konto-Auszüge", "ARGUS/Auszug'", "ARGUS//2026"])
def test_a_statement_folder_that_cannot_be_looked_up_is_refused(folder: str) -> None:
    with pytest.raises(JobSettingsError, match="ARGUS_STATEMENT_FOLDER"):
        statement_services_from_env({**DEMO, "ARGUS_STATEMENT_FOLDER": folder})


@pytest.mark.req("BA-01")
def test_statement_senders_that_are_not_addresses_are_refused() -> None:
    with pytest.raises(JobSettingsError, match="ARGUS_STATEMENT_SENDERS"):
        services_from_env({**DEMO, "ARGUS_STATEMENT_SENDERS": "benjamin"})


# The digest address at go live (#167, EX-03)

LIVE_DIGEST = {
    "ARGUS_DEMO": "false",
    "ARGUS_GRAPH_URL": "https://graph.microsoft.com/v1.0",
    "ARGUS_MAILBOX": "accounts@firma.example",
    "ARGUS_DATABASE_URL": "postgresql+psycopg://argus_app@localhost:5432/argus",
    "ARGUS_APP_URL": "https://argus.example",
}
DIGEST_ADDRESS = "person@firma.example"


def safe_text(caplog: pytest.LogCaptureFixture) -> str:
    """The log as Azure sees it: every line through the safe formatter of the jobs."""
    formatter = SafeFormatter("%(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


class FakeGraph:
    def close(self) -> None:
        pass


@pytest.fixture
def graph_calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Records each Graph client the jobs build; a client is what could send an email."""
    calls: list[bool] = []

    def graph_client(self: Connections, *args: Any, digest: bool = False, **kwargs: Any) -> Any:
        calls.append(digest)
        return FakeGraph()

    monkeypatch.setattr(Connections, "graph_client", graph_client)
    return calls


@pytest.mark.req("EX-03")
def test_live_with_a_digest_address_builds_the_digest_job(graph_calls: list[bool]) -> None:
    services = digest_services_from_env({**LIVE_DIGEST, "ARGUS_DIGEST_TO": DIGEST_ADDRESS})
    try:
        assert graph_calls == [True]
        assert services.engine.url.database == "argus"
    finally:
        services.close()


@pytest.mark.req("EX-03", "NF-03")
@pytest.mark.parametrize("value", [None, "", "   "])
def test_live_or_shadow_without_a_digest_address_fails_with_the_reason_and_sends_nothing(
    value: str | None, graph_calls: list[bool], caplog: pytest.LogCaptureFixture
) -> None:
    environ = dict(LIVE_DIGEST)
    if value is not None:
        environ["ARGUS_DIGEST_TO"] = value

    with caplog.at_level(logging.INFO):
        assert main(["digest"], environ=environ) == 1

    assert graph_calls == []
    assert outcome_lines(caplog) == [f"{FAILED_MARK} digest"]
    text = safe_text(caplog)
    assert "The digest job cannot run: set ARGUS_DIGEST_TO before the digest job can run" in text
    assert "docs/runbooks/go-live.md" in text


@pytest.mark.req("EX-03", "NF-03")
@pytest.mark.parametrize(
    "value",
    ["one@firma.example, two@firma.example", "Benjamin <person@firma.example>", "person"],
)
def test_more_than_one_digest_address_fails_without_writing_it(
    value: str, graph_calls: list[bool], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assert main(["digest"], environ={**LIVE_DIGEST, "ARGUS_DIGEST_TO": value}) == 1

    assert graph_calls == []
    text = safe_text(caplog)
    assert "ARGUS_DIGEST_TO must be exactly one email address" in text
    assert value not in caplog.text


@pytest.mark.req("EX-03")
def test_demo_mode_uses_the_demo_digest_address() -> None:
    environ = {
        **DEMO,
        "ARGUS_DIGEST_TO": "digest@maxasp.example",
        "ARGUS_APP_URL": "https://argus.example",
    }
    services = digest_services_from_env(environ)
    try:
        assert services.graph.digest_to == "digest@maxasp.example"
        assert services.graph.forward_to is None
    finally:
        services.close()


@pytest.mark.req("EX-03")
def test_demo_mode_without_a_digest_address_fails_too(caplog: pytest.LogCaptureFixture) -> None:
    environ = {**DEMO, "ARGUS_APP_URL": "https://argus.example"}

    with caplog.at_level(logging.INFO):
        assert main(["digest"], environ=environ) == 1

    assert "set ARGUS_DIGEST_TO before the digest job can run" in safe_text(caplog)


@pytest.mark.req("IN-01", "NF-03")
def test_any_other_error_is_still_logged_as_its_type_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(environ: Mapping[str, str]) -> Services:
        raise RuntimeError("row with sender person@firma.example")

    with caplog.at_level(logging.INFO):
        run_job("sweep", DEMO, build=broken)

    text = safe_text(caplog)
    assert "error: RuntimeError" in text
    assert "person@firma.example" not in text
