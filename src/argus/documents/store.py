"""Blob storage for original documents, addressed by their SHA-256 fingerprint.

A file is stored under ``sha256/<fingerprint>``. The same bytes always land on the same name,
so storing a file twice finds the first copy and never replaces it. In production the container
also has a locked retention policy (ADR 0001), so even an administrator cannot change a file;
this module does not rely on that and refuses to overwrite on its own.

Nothing here touches the database. Store the files first, then record them in a short
``unit_of_work`` (see :mod:`argus.documents.records`), because a slow upload must not hold the
audit lock (review of PR #79).
"""

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings

from argus.secrets import SecretStore

CONNECTION_STRING_SECRET = "ARGUS_DOCUMENTS_CONNECTION_STRING"  # noqa: S105 (a name, no value)
DEFAULT_CONTAINER = "documents"

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class DocumentNotFound(LookupError):
    """No file with this fingerprint is in the store."""


class FingerprintMismatch(Exception):
    """A stored file no longer matches its fingerprint. It was changed or damaged after it was
    stored, which must never happen (NF-01). Stop and tell a person."""


@dataclass(frozen=True)
class StoredDocument:
    sha256: str
    """SHA-256 of the exact bytes, 64 lower case hex characters."""
    location: str
    """Name of the file in the blob container, always ``sha256/<sha256>``."""
    size: int
    content_type: str


class DocumentStore(Protocol):
    def put(self, data: bytes, content_type: str) -> StoredDocument:
        """Store the bytes as they are. Safe to repeat: the same bytes give back the copy that
        is already there, and an existing file is never replaced."""
        ...

    def get(self, sha256: str) -> bytes:
        """Read a file back. Raises :class:`FingerprintMismatch` if its bytes changed."""
        ...


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def location_of(sha256: str) -> str:
    if not _FINGERPRINT.match(sha256):
        raise ValueError("a fingerprint is 64 lower case hex characters")
    return f"sha256/{sha256}"


def _checked(sha256: str, data: bytes) -> bytes:
    if fingerprint(data) != sha256:
        raise FingerprintMismatch(f"the file stored as {sha256} no longer matches its fingerprint")
    return data


class AzureDocumentStore:
    """Azure Blob Storage, or Azurite locally and in CI."""

    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    @classmethod
    def from_connection_string(
        cls, connection_string: str, container: str = DEFAULT_CONTAINER
    ) -> "AzureDocumentStore":
        service = BlobServiceClient.from_connection_string(connection_string)
        return cls(service.get_container_client(container))

    @classmethod
    def from_secrets(
        cls, secrets: SecretStore, container: str = DEFAULT_CONTAINER
    ) -> "AzureDocumentStore":
        """The connection string comes from Key Vault in Azure (NF-06), never from the repo."""
        connection_string = secrets.get(CONNECTION_STRING_SECRET).get_secret_value()
        return cls.from_connection_string(connection_string, container)

    @property
    def container_name(self) -> str:
        return self._container.container_name

    def create_container_if_missing(self) -> None:
        """For local runs and tests. In Azure the container and its retention lock are set up
        with the infrastructure, not by the application."""
        with suppress(ResourceExistsError):
            self._container.create_container()

    def put(self, data: bytes, content_type: str) -> StoredDocument:
        sha256 = fingerprint(data)
        location = location_of(sha256)
        blob = self._container.get_blob_client(location)
        try:
            blob.upload_blob(
                data,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
                metadata={"sha256": sha256},
            )
        except HttpResponseError as error:
            # 409: the name is taken (or locked by the retention policy). The name is the
            # fingerprint, so the file there must hold the same bytes; check it, never replace it.
            if error.status_code != 409:
                raise
            _checked(sha256, blob.download_blob().readall())
            stored_type = blob.get_blob_properties().content_settings.content_type
            return StoredDocument(sha256, location, len(data), stored_type or content_type)
        return StoredDocument(sha256, location, len(data), content_type)

    def get(self, sha256: str) -> bytes:
        try:
            data = self._container.download_blob(location_of(sha256)).readall()
        except ResourceNotFoundError as error:
            raise DocumentNotFound(sha256) from error
        return _checked(sha256, data)


class MemoryDocumentStore:
    """Keeps files in memory, with the same rules. For tests of the modules that use the store."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    def put(self, data: bytes, content_type: str) -> StoredDocument:
        sha256 = fingerprint(data)
        location = location_of(sha256)
        if location in self.files:
            existing, stored_type = self.files[location]
            _checked(sha256, existing)
            return StoredDocument(sha256, location, len(data), stored_type)
        self.files[location] = (bytes(data), content_type)
        return StoredDocument(sha256, location, len(data), content_type)

    def get(self, sha256: str) -> bytes:
        try:
            data, _ = self.files[location_of(sha256)]
        except KeyError as error:
            raise DocumentNotFound(sha256) from error
        return _checked(sha256, data)
