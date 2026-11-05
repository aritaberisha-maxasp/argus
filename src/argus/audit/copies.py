"""Where the nightly copies live and what is in them.

A copy is one JSON Lines file per night, ``audit-log/<YYYY-MM-DD>.jsonl``, in the document
container of ADR 0001. In production that container has the locked retention policy, so a copy
can be neither changed nor deleted. This module does not rely on that: it never replaces a file
that is already there.

The first line describes the copy::

    {"format": "argus-audit-copy", "version": 1, "night": "2026-11-03",
     "first_seq": 41, "last_seq": 57, "event_count": 17, "prev_hash": "<hex>"}

``prev_hash`` is the hash of the event just before ``first_seq`` (``"00"`` before the very first
event, like the chain itself). Then comes one line per event, in chain order, with exactly the
columns of ``audit_event`` (:data:`EVENT_COLUMNS`) and nothing else. So a copy holds only what
the audit log already holds: no free text and no full IBANs (NF-03), because the log has none.
When the night had no new events, ``last_seq`` is ``first_seq - 1`` and the copy is just the
first line, which still records that the job ran and where the chain ended.
"""

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from azure.core.exceptions import HttpResponseError, ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings

from argus.documents.store import CONNECTION_STRING_SECRET, DEFAULT_CONTAINER
from argus.secrets import SecretStore

PREFIX = "audit-log/"
FORMAT = "argus-audit-copy"
VERSION = 1
CONTENT_TYPE = "application/x-ndjson"
GENESIS_HASH = "00"

# Every column of audit_event, in table order. A test compares this with the table, so a new
# column cannot be left out of the copies (or put in without thinking about what it holds).
EVENT_COLUMNS = (
    "id",
    "chain_seq",
    "hash_version",
    "occurred_at",
    "case_id",
    "email_id",
    "statement_id",
    "exception_id",
    "payment_run_id",
    "event_type",
    "actor_type",
    "actor",
    "from_status",
    "to_status",
    "details",
    "prev_hash",
    "hash",
)

_NAME = re.compile(r"^audit-log/(\d{4}-\d{2}-\d{2})\.jsonl$")


class CopyUnreadable(ValueError):
    """A file in the copy folder is not a well formed copy."""


@dataclass(frozen=True)
class CopyHeader:
    night: date
    first_seq: int
    last_seq: int
    event_count: int
    prev_hash: str


@dataclass(frozen=True)
class AuditCopy:
    name: str
    header: CopyHeader
    events: list[dict[str, Any]]


def name_for(night: date) -> str:
    return f"{PREFIX}{night.isoformat()}.jsonl"


def night_of(name: str) -> date | None:
    """The night a copy file is for, or None for a name that is not a copy."""
    found = _NAME.match(name)
    return date.fromisoformat(found.group(1)) if found else None


def event_record(row: Any) -> dict[str, Any]:
    """One ``audit_event`` row as it goes into a copy. The same function prepares the database
    row when the check compares it, so both sides look alike when nothing changed."""
    record: dict[str, Any] = {}
    for column in EVENT_COLUMNS:
        value = row[column]
        if isinstance(value, bytes | memoryview):
            value = bytes(value).hex()
        elif isinstance(value, datetime):
            value = value.astimezone(UTC).isoformat()
        record[column] = value
    return record


def encode(header: CopyHeader, events: list[dict[str, Any]]) -> bytes:
    first = {
        "format": FORMAT,
        "version": VERSION,
        "night": header.night.isoformat(),
        "first_seq": header.first_seq,
        "last_seq": header.last_seq,
        "event_count": header.event_count,
        "prev_hash": header.prev_hash,
    }
    lines = [json.dumps(first, sort_keys=True)]
    lines += [json.dumps(event, sort_keys=True, ensure_ascii=False) for event in events]
    return ("\n".join(lines) + "\n").encode("utf-8")


def decode(name: str, data: bytes) -> AuditCopy:
    """Read a copy. Raises :class:`CopyUnreadable` if the file is not one, so the check can
    report it instead of stopping."""
    try:
        lines = data.decode("utf-8").splitlines()
        first = json.loads(lines[0])
        if first.get("format") != FORMAT or first.get("version") != VERSION:
            raise CopyUnreadable(f"{name} is not an audit copy of version {VERSION}")
        header = CopyHeader(
            night=date.fromisoformat(first["night"]),
            first_seq=int(first["first_seq"]),
            last_seq=int(first["last_seq"]),
            event_count=int(first["event_count"]),
            prev_hash=str(first["prev_hash"]),
        )
        events = [json.loads(line) for line in lines[1:] if line]
    except CopyUnreadable:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise CopyUnreadable(f"{name} cannot be read: {type(error).__name__}") from error
    if not all(isinstance(event, dict) for event in events):
        raise CopyUnreadable(f"{name} has a line that is not an event")
    if night_of(name) != header.night:
        raise CopyUnreadable(f"{name} says it is for the night {header.night}")
    return AuditCopy(name, header, events)


class CopyStore(Protocol):
    def names(self) -> list[str]:
        """Every file in the copy folder (``audit-log/``), in any order."""
        ...

    def read(self, name: str) -> bytes: ...

    def write_new(self, name: str, data: bytes) -> bool:
        """Write a file that is not there yet. Returns False, and changes nothing, if a file of
        that name already exists."""
        ...


class AzureCopyStore:
    """Azure Blob Storage, or Azurite locally and in CI. Uses the document container, which
    carries the locked retention policy in production."""

    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    @classmethod
    def from_connection_string(
        cls, connection_string: str, container: str = DEFAULT_CONTAINER
    ) -> "AzureCopyStore":
        service = BlobServiceClient.from_connection_string(connection_string)
        return cls(service.get_container_client(container))

    @classmethod
    def from_secrets(
        cls, secrets: SecretStore, container: str = DEFAULT_CONTAINER
    ) -> "AzureCopyStore":
        """The same connection secret as the document store, from Key Vault in Azure."""
        connection_string = secrets.get(CONNECTION_STRING_SECRET).get_secret_value()
        return cls.from_connection_string(connection_string, container)

    def create_container_if_missing(self) -> None:
        """For local runs and tests. In Azure the infrastructure creates the container."""
        with suppress(ResourceExistsError):
            self._container.create_container()

    def names(self) -> list[str]:
        return list(self._container.list_blob_names(name_starts_with=PREFIX))

    def read(self, name: str) -> bytes:
        data: bytes = self._container.download_blob(name).readall()
        return data

    def write_new(self, name: str, data: bytes) -> bool:
        try:
            self._container.upload_blob(
                name,
                data,
                overwrite=False,
                content_settings=ContentSettings(content_type=CONTENT_TYPE),
            )
        except HttpResponseError as error:
            # 409: the name is taken (or locked by the retention policy). Never replace it.
            if error.status_code != 409:
                raise
            return False
        return True


class MemoryCopyStore:
    """Keeps copies in memory, with the same rule. For unit tests."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def names(self) -> list[str]:
        return [name for name in self.files if name.startswith(PREFIX)]

    def read(self, name: str) -> bytes:
        return self.files[name]

    def write_new(self, name: str, data: bytes) -> bool:
        if name in self.files:
            return False
        self.files[name] = bytes(data)
        return True
