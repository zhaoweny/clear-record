"""The explicit re-run scope: parsing, selection and refusal (ADR-0018).

A scope is an *assertion* about which chunks may be reused, so the two failure
modes that matter are both refusals: a range that cannot be read must not decay
into "everything", and a scope that selects nothing must not decay into a no-op.
"""

from __future__ import annotations

import pytest

from clear_record.core import (
    ChunkScope,
    ScopeError,
    format_seconds,
    i18n,
    parse_time_range,
)


def test_no_scope_input_is_none_not_an_empty_scope() -> None:
    """Absent scope and empty scope must not be confused at the call site."""
    assert ChunkScope.parse(None, None) is None
    assert ChunkScope.parse((), "") is None
    assert ChunkScope.parse(("  ",), None) is None


def test_source_names_are_stripped_and_deduplicated_in_order() -> None:
    scope = ChunkScope.parse((" b ", "a", "b"), None)
    assert scope is not None
    assert scope.sources == ("b", "a")


def test_two_clock_fields_are_hours_and_minutes() -> None:
    """`12:30-18:00` is the example a multi-hour tape is named in."""
    assert parse_time_range("12:30-18:00") == (12 * 3600 + 30 * 60, 18 * 3600)
    assert parse_time_range("0:00:30-0:01") == (30.0, 60.0)
    # A single field is plain seconds, so a sub-minute range stays expressible.
    assert parse_time_range("5-6") == (5.0, 6.0)
    assert parse_time_range("1:02:03.5-1:02:05") == (3723.5, 3725.0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "12:30",
        "12:30-",
        "-18:00",
        "12:30-18:00-19:00",
        "noon-18:00",
        "12:70-13:00",
        "0:00:99-0:01",
        "18:00-12:30",
        "5-5",
    ],
)
def test_an_unreadable_range_refuses_instead_of_widening(text: str) -> None:
    """Every malformed shape is a ScopeError, never a silent full re-decode."""
    with pytest.raises(ScopeError):
        parse_time_range(text)


def test_a_scope_selects_a_chunk_by_overlap_not_containment() -> None:
    scope = ChunkScope.parse(None, "5-6")
    assert scope is not None
    assert scope.selects_chunk(4.0, 7.0) is True  # straddles the range
    assert scope.selects_chunk(2.0, 5.0) is False  # ends exactly at the start
    assert scope.selects_chunk(6.0, 8.0) is False  # starts exactly at the end


def test_a_source_scope_selects_every_chunk_of_that_source() -> None:
    scope = ChunkScope.parse(("a",), None)
    assert scope is not None
    assert scope.selects("a", 0.0, 1.0) is True
    assert scope.selects("b", 0.0, 1.0) is False


def test_no_scope_selects_every_chunk() -> None:
    assert ChunkScope().selects("anything", 0.0, 1.0) is True
    assert ChunkScope().is_empty is True


def test_describe_names_what_is_selected() -> None:
    scope = ChunkScope.parse(("a", "b"), "12:30-18:00")
    assert scope is not None
    assert scope.describe() == "sources=a,b range=12:30:00-18:00:00"


def test_format_seconds_rounds_to_whole_seconds() -> None:
    assert format_seconds(3725.4) == "1:02:05"
    assert format_seconds(-3) == "0:00:00"


def test_a_scope_error_is_translated_in_a_chinese_catalog() -> None:
    """A malformed scope is a user-facing usage error, not an internal one."""
    i18n.install("zh_CN")
    with pytest.raises(ScopeError) as excinfo:
        parse_time_range("noon-18:00")
    assert "无法读取重跑范围" in str(excinfo.value)
    assert "cannot read" not in str(excinfo.value)
