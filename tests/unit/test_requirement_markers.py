import pytest

from tests.conftest import known_requirement_ids, req_marker_problems


def test_catalog_ids_are_found() -> None:
    assert {"IN-01", "NF-04", "PY-13", "VM-04"} <= known_requirement_ids()


@pytest.mark.req("NF-01")
def test_req_marker_accepts_a_catalog_id() -> None:
    pass


def test_unknown_id_is_reported() -> None:
    problems = req_marker_problems([("t::typo", ("IN-01", "XX-99"))], {"IN-01"})

    assert problems == ["t::typo: unknown requirement ID 'XX-99'"]


def test_empty_marker_is_reported() -> None:
    assert req_marker_problems([("t::empty", ())], {"IN-01"}) == [
        "t::empty: req marker without any ID"
    ]
