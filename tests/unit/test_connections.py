"""ARGUS talks either to the stand ins or to the real systems, never to both (#124, ADR 0009).

Covers: IN-01, NF-04, NF-06
"""

from collections.abc import Mapping

import pytest
from fastapi import FastAPI

from argus.app import create_app
from argus.connections import (
    ConnectionSettingsError,
    NotConnected,
    connections_from_env,
    demo_rule_problems,
    is_real_address,
    is_standin_address,
)
from argus.intake.graph import GraphClient
from argus.wise.client import WiseClient

# Values that must never show up in an error message. Built at runtime so the secret scan does
# not take them for real tokens.
REAL_TOKEN = "real-" + "x" * 24
STANDIN_TOKEN = "standin-" + "y" * 24

DEMO = {
    "ARGUS_DEMO": "true",
    "ARGUS_GRAPH_URL": "http://ca-argus-standin-graph/v1.0",
    "ARGUS_MAILBOX": "accounts@maxasp.example",
    "ARGUS_DATEV_URL": "http://ca-argus-standin-datev",
    "ARGUS_DATEV_CLIENT": "muster-gmbh",
    "ARGUS_WISE_URL": "http://ca-argus-standin-wise",
    "ARGUS_WISE_PROFILE_ID": "16000001",
    "ARGUS_GRAPH_STANDIN_TOKEN": "header.payload.fake-signature",
    "ARGUS_WISE_STANDIN_TOKEN": STANDIN_TOKEN,
}

LIVE = {
    "ARGUS_DEMO": "false",
    "ARGUS_GRAPH_URL": "https://graph.microsoft.com/v1.0",
    "ARGUS_MAILBOX": "accounts@maxasp.example",
    "ARGUS_DATEV_URL": "https://datev.maxasp.example",
    "ARGUS_DATEV_CLIENT": "12345-67890",
    "ARGUS_WISE_URL": "https://api.wise.com",
    "ARGUS_WISE_PROFILE_ID": "16000001",
    "WISE_API_TOKEN": REAL_TOKEN,
}


def with_(base: Mapping[str, str], **changes: str) -> dict[str, str]:
    return {**base, **changes}


@pytest.mark.parametrize(
    "url",
    [
        "http://ca-argus-standin-graph/v1.0",
        "http://ca-argus-standin-datev:58452",
        "http://localhost:8081/v1.0",
        "http://127.0.0.1:8080",
    ],
)
def test_stand_in_addresses(url: str) -> None:
    assert is_standin_address(url)
    assert not is_real_address(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://graph.microsoft.com/v1.0",
        "https://api.wise.com",
        "https://datev.maxasp.example",
    ],
)
def test_real_addresses(url: str) -> None:
    assert is_real_address(url)
    assert not is_standin_address(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.microsoft.com/v1.0",  # a real host over plain http is neither
        "https://localhost:8081",  # https to this machine is not real
        "ftp://ca-argus-standin-graph",
        "",
    ],
)
def test_neither_stand_in_nor_real(url: str) -> None:
    assert not is_standin_address(url)
    assert not is_real_address(url)


@pytest.mark.req("IN-01", "NF-06")
def test_the_demo_settings_pass() -> None:
    connections = connections_from_env(DEMO)
    assert connections.demo
    assert connections.wise_profile_id == 16_000_001


@pytest.mark.req("IN-01", "NF-06")
def test_the_live_settings_pass() -> None:
    assert not connections_from_env(LIVE).demo


def test_nothing_set_means_not_connected_and_not_demo() -> None:
    connections = connections_from_env({})
    assert not connections.demo
    with pytest.raises(NotConnected):
        connections.graph_client()
    with pytest.raises(NotConnected):
        connections.datev_gateway()
    with pytest.raises(NotConnected):
        connections.wise_client()


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        (
            "ARGUS_GRAPH_URL",
            "https://graph.microsoft.com/v1.0",
            "demo mode is on but ARGUS_GRAPH_URL is not a stand in address",
        ),
        (
            "ARGUS_DATEV_URL",
            "https://datev.maxasp.example",
            "demo mode is on but ARGUS_DATEV_URL is not a stand in address",
        ),
        (
            "ARGUS_WISE_URL",
            "https://api.wise.com",
            "demo mode is on but ARGUS_WISE_URL is not a stand in address",
        ),
        (
            "WISE_API_TOKEN",
            REAL_TOKEN,
            "demo mode is on but the real secret WISE_API_TOKEN is set",
        ),
        (
            "ARGUS_DATEV_CLIENT_SECRET",
            REAL_TOKEN,
            "demo mode is on but the real secret ARGUS_DATEV_CLIENT_SECRET is set",
        ),
    ],
)
def test_demo_mode_refuses_a_real_address_or_secret(name: str, value: str, problem: str) -> None:
    with pytest.raises(ConnectionSettingsError) as refused:
        connections_from_env(with_(DEMO, **{name: value}))
    assert str(refused.value) == problem
    assert value not in str(refused.value)


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        (
            "ARGUS_GRAPH_URL",
            "http://ca-argus-standin-graph/v1.0",
            "demo mode is off but ARGUS_GRAPH_URL is not an https address",
        ),
        (
            "ARGUS_WISE_URL",
            "http://ca-argus-standin-wise",
            "demo mode is off but ARGUS_WISE_URL is not an https address",
        ),
        (
            "ARGUS_WISE_STANDIN_TOKEN",
            STANDIN_TOKEN,
            "demo mode is off but the stand in token ARGUS_WISE_STANDIN_TOKEN is set",
        ),
        (
            "ARGUS_GRAPH_STANDIN_TOKEN",
            STANDIN_TOKEN,
            "demo mode is off but the stand in token ARGUS_GRAPH_STANDIN_TOKEN is set",
        ),
    ],
)
def test_live_mode_refuses_a_stand_in_address_or_token(name: str, value: str, problem: str) -> None:
    with pytest.raises(ConnectionSettingsError) as refused:
        connections_from_env(with_(LIVE, **{name: value}))
    assert str(refused.value) == problem
    assert value not in str(refused.value)


def test_unset_demo_counts_as_off() -> None:
    settings = dict(LIVE)
    del settings["ARGUS_DEMO"]
    assert not connections_from_env(settings).demo
    assert demo_rule_problems({"ARGUS_WISE_STANDIN_TOKEN": STANDIN_TOKEN}) == [
        "demo mode is off but the stand in token ARGUS_WISE_STANDIN_TOKEN is set"
    ]


@pytest.mark.parametrize("value", ["maybe", "ture", "2"])
def test_an_unclear_demo_setting_is_refused(value: str) -> None:
    with pytest.raises(ConnectionSettingsError, match="ARGUS_DEMO must be true or false"):
        connections_from_env({"ARGUS_DEMO": value})


def test_every_problem_is_named_at_once() -> None:
    settings = with_(DEMO, ARGUS_WISE_URL="https://api.wise.com", WISE_API_TOKEN=REAL_TOKEN)
    with pytest.raises(ConnectionSettingsError) as refused:
        connections_from_env(settings)
    assert "ARGUS_WISE_URL" in str(refused.value)
    assert "WISE_API_TOKEN" in str(refused.value)


def test_the_profile_must_be_a_number() -> None:
    with pytest.raises(ConnectionSettingsError, match="ARGUS_WISE_PROFILE_ID must be a number"):
        connections_from_env(with_(DEMO, ARGUS_WISE_PROFILE_ID="max-asp"))


@pytest.mark.req("IN-01")
def test_demo_mode_builds_the_clients_for_the_stand_ins() -> None:
    connections = connections_from_env(DEMO)
    graph = connections.graph_client()
    wise = connections.wise_client()
    datev = connections.datev_gateway()
    try:
        assert isinstance(graph, GraphClient)
        assert "ca-argus-standin-graph" in repr(graph)
        assert isinstance(wise, WiseClient)
        assert "ca-argus-standin-wise" in repr(wise)
        assert datev is not None
    finally:
        graph._http.close()
        wise.close()


def test_demo_mode_needs_the_stand_in_tokens() -> None:
    settings = dict(DEMO)
    del settings["ARGUS_GRAPH_STANDIN_TOKEN"]
    del settings["ARGUS_WISE_STANDIN_TOKEN"]
    connections = connections_from_env(settings)
    with pytest.raises(ConnectionSettingsError, match="ARGUS_GRAPH_STANDIN_TOKEN"):
        connections.graph_client()
    with pytest.raises(ConnectionSettingsError, match="ARGUS_WISE_STANDIN_TOKEN"):
        connections.wise_client()


def test_live_mode_builds_the_wise_client_with_the_real_token() -> None:
    wise = connections_from_env(LIVE).wise_client()
    try:
        assert "api.wise.com" in repr(wise)
    finally:
        wise.close()


def test_live_mode_has_no_mailbox_sign_in_yet() -> None:
    with pytest.raises(NotConnected, match="Microsoft 365 sign in"):
        connections_from_env(LIVE).graph_client()


def test_the_stand_in_tokens_never_show_in_a_repr() -> None:
    connections = connections_from_env(DEMO)
    assert STANDIN_TOKEN not in repr(connections)
    assert REAL_TOKEN not in repr(connections_from_env(LIVE))


@pytest.mark.req("NF-04", "NF-06")
@pytest.mark.parametrize(
    "settings",
    [
        with_(DEMO, WISE_API_TOKEN=REAL_TOKEN),
        with_(DEMO, ARGUS_GRAPH_URL="https://graph.microsoft.com/v1.0"),
        with_(LIVE, ARGUS_DATEV_URL="http://ca-argus-standin-datev"),
        with_(LIVE, ARGUS_GRAPH_STANDIN_TOKEN=STANDIN_TOKEN),
    ],
)
def test_the_app_refuses_to_start_when_the_two_worlds_mix(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str]
) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConnectionSettingsError):
        create_app()


@pytest.mark.parametrize("settings", [DEMO, LIVE])
def test_the_app_starts_with_either_world(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str]
) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert isinstance(create_app(), FastAPI)
