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

from argus.connections import ConnectionSettingsError, NotConnected
from argus.intake import jobs
from argus.intake.jobs import (
    FAILED_MARK,
    OK_MARK,
    IntakeSettings,
    JobSettingsError,
    Services,
    main,
    run_job,
    services_from_env,
)
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
        def graph_client(self) -> Graph:
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
        engine = graph = archive = sorting = None

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
