"""Shared pytest setup.

Every requirement ID used in a ``@pytest.mark.req(...)`` marker must exist in the
requirements catalog, so a typo fails the run instead of silently breaking traceability.
"""

import re
from collections.abc import Iterable
from pathlib import Path

import pytest

CATALOG = Path(__file__).resolve().parent.parent / "docs" / "requirements" / "requirements.md"
REQ_ID = re.compile(r"\b[A-Z]{2}-\d+\b")


def known_requirement_ids() -> set[str]:
    return set(REQ_ID.findall(CATALOG.read_text(encoding="utf-8")))


def req_marker_problems(
    test_markers: Iterable[tuple[str, tuple[object, ...]]], known: set[str]
) -> list[str]:
    """List problems for (test id, marker args) pairs: an empty marker or an unknown ID."""
    problems = []
    for nodeid, ids in test_markers:
        if not ids:
            problems.append(f"{nodeid}: req marker without any ID")
        problems += [
            f"{nodeid}: unknown requirement ID {req_id!r}" for req_id in ids if req_id not in known
        ]
    return problems


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    test_markers = [
        (item.nodeid, marker.args) for item in items for marker in item.iter_markers(name="req")
    ]
    problems = req_marker_problems(test_markers, known_requirement_ids())
    if problems:
        raise pytest.UsageError("\n".join(problems))
