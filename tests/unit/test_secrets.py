"""Credentials come from the secret store and never show in output (NF-06)."""

import pytest

from argus.secrets import EnvironmentSecretStore, MissingSecretError


@pytest.mark.req("NF-06")
def test_a_secret_is_read_but_printed_masked() -> None:
    secret = EnvironmentSecretStore({"SOME_TOKEN": "  value-123  "}).get("SOME_TOKEN")
    assert secret.get_secret_value() == "value-123"
    assert "value-123" not in f"{secret} {secret!r}"


@pytest.mark.req("NF-06")
@pytest.mark.parametrize("environ", [{}, {"SOME_TOKEN": ""}, {"SOME_TOKEN": "   "}])
def test_a_missing_or_empty_secret_is_an_error(environ: dict[str, str]) -> None:
    with pytest.raises(MissingSecretError, match="SOME_TOKEN"):
        EnvironmentSecretStore(environ).get("SOME_TOKEN")
