"""Reading credentials from the secret store (ADR 0001, NF-06).

In Azure the secrets live in Key Vault. Container Apps hands them to ARGUS as
environment variables through Key Vault references, so the app reads them from the
environment and never from a file in the repo. Values come back as ``SecretStr``,
which prints as ``**********`` in logs, reprs and error messages.
"""

import os
from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr


class MissingSecretError(LookupError):
    """A secret ARGUS needs is not in the secret store. The message names it, never a value."""


class SecretStore(Protocol):
    def get(self, name: str) -> SecretStr: ...


class EnvironmentSecretStore:
    """Secrets from environment variables, filled from Key Vault in Azure."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def get(self, name: str) -> SecretStr:
        value = self._environ.get(name, "").strip()
        if not value:
            raise MissingSecretError(f"secret {name} is not set in the secret store")
        return SecretStr(value)
