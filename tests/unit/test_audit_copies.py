"""The file format of the nightly audit copy (issue #83). No database or storage needed."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from argus.audit.copies import (
    CopyHeader,
    CopyUnreadable,
    MemoryCopyStore,
    decode,
    encode,
    event_record,
    name_for,
    night_of,
)

NIGHT = date(2026, 11, 3)


def row(seq: int) -> dict[str, object]:
    return {
        "id": seq,
        "chain_seq": seq,
        "hash_version": 1,
        "occurred_at": datetime(2026, 11, 3, 1, 0, tzinfo=timezone(timedelta(hours=1))),
        "case_id": None,
        "email_id": 7,
        "statement_id": None,
        "exception_id": None,
        "payment_run_id": None,
        "event_type": "email_classified",
        "actor_type": "system",
        "actor": "intake",
        "from_status": None,
        "to_status": None,
        "details": {"route": "manual_review", "note": "Umlaut ä stays"},
        "prev_hash": bytes([seq - 1]),
        "hash": memoryview(bytes([seq])),
        "not_a_column": "left out",
    }


@pytest.mark.req("NF-01")
def test_an_event_keeps_exactly_the_audit_columns_with_hashes_as_hex_and_time_in_utc() -> None:
    record = event_record(row(2))

    assert "not_a_column" not in record
    assert record["prev_hash"] == "01"
    assert record["hash"] == "02"
    assert record["occurred_at"] == datetime(2026, 11, 3, 0, 0, tzinfo=UTC).isoformat()


@pytest.mark.req("NF-01")
def test_a_copy_reads_back_as_written() -> None:
    events = [event_record(row(1)), event_record(row(2))]
    header = CopyHeader(NIGHT, 1, 2, 2, "00")

    copy = decode(name_for(NIGHT), encode(header, events))

    assert copy.header == header
    assert copy.events == events
    assert encode(header, events).decode().count("\n") == 3


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"", "IndexError"),
        (b"\xff\xfe", "UnicodeDecodeError"),
        (b"[1]\n", "AttributeError"),
        (b'{"format": "something else", "version": 1}\n', "is not an audit copy"),
        (b'{"format": "argus-audit-copy", "version": 1}\n', "KeyError"),
    ],
)
@pytest.mark.req("NF-01")
def test_a_file_that_is_not_a_copy_is_refused(data: bytes, problem: str) -> None:
    with pytest.raises(CopyUnreadable, match=problem):
        decode(name_for(NIGHT), data)


@pytest.mark.req("NF-01")
def test_a_copy_must_be_for_the_night_in_its_name() -> None:
    data = encode(CopyHeader(NIGHT, 1, 0, 0, "00"), [])

    with pytest.raises(CopyUnreadable, match="says it is for the night 2026-11-03"):
        decode(name_for(date(2026, 11, 4)), data)


@pytest.mark.req("NF-01")
def test_a_copy_line_that_is_not_an_event_is_refused() -> None:
    data = encode(CopyHeader(NIGHT, 1, 1, 1, "00"), []) + b"[1, 2]\n"

    with pytest.raises(CopyUnreadable, match="not an event"):
        decode(name_for(NIGHT), data)


def test_only_copy_names_have_a_night() -> None:
    assert night_of("audit-log/2026-11-03.jsonl") == NIGHT
    assert night_of("audit-log/2026-11-03.json") is None
    assert night_of("sha256/" + "a" * 64) is None


def test_the_memory_store_never_replaces_a_file() -> None:
    store = MemoryCopyStore()

    assert store.write_new(name_for(NIGHT), b"first")
    assert not store.write_new(name_for(NIGHT), b"second")
    assert store.read(name_for(NIGHT)) == b"first"
    store.files["sha256/x"] = b"document"
    assert store.names() == [name_for(NIGHT)]
