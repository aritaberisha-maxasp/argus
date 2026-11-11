from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from argus import __version__
from argus.app import create_app, migrations_head


def test_health_reports_ok_version_and_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_BUILD", "abc123")
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "build": "abc123"}


def test_health_says_dev_when_no_build_is_baked_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGUS_BUILD", raising=False)

    assert TestClient(create_app()).get("/health").json()["build"] == "dev"


def test_migrations_head_is_found_next_to_the_code() -> None:
    assert migrations_head() is not None


def test_database_health_is_503_without_a_database() -> None:
    def no_database() -> Engine:
        raise RuntimeError("ARGUS_DATABASE_URL is not set")

    response = TestClient(create_app(no_database)).get("/health/database")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unreachable",
        "role": None,
        "revision": None,
        "head": migrations_head(),
    }


def test_database_health_hides_the_reason(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> Any:
        raise RuntimeError("could not reach host somewhere")

    response = TestClient(create_app(broken)).get("/health/database")

    assert "somewhere" not in response.text
    assert "somewhere" in caplog.text
