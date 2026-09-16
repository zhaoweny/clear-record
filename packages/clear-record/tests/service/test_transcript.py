"""Reading a meeting's transcript: the text an agent reasons about.

The transcript is workspace files, not registry state, so these exercise the
service reader directly (temp workspace, no run, no backend): reconciled record
preferred, raw segments fallback, and the slice cursor for a long tape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.core import RecordDocument, Segment, to_dict, write_json
from clear_record.service import Registry, read_transcript


def _registry(tmp_path: Path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    return registry, meeting, workspace


def _write_record(workspace: Path, segments: list[Segment]) -> None:
    write_json(
        workspace / "record.json",
        RecordDocument(sources=(), alignment=None, segments=tuple(segments)),
    )


def test_reads_the_reconciled_record_as_text(tmp_path: Path) -> None:
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(
        workspace,
        [
            Segment(start=0.0, end=1.5, text="hello", source="a", speaker="Alice"),
            Segment(start=65.5, end=70.0, text="world", source="b"),
        ],
    )

    page = read_transcript(meeting)
    assert page.source == "record"
    assert page.total == 2
    assert page.returned == 2
    assert page.next is None
    assert page.text == ("00:00:00.000 [Alice] hello\n00:01:05.500 [b] world")


def test_a_slice_pages_a_long_tape(tmp_path: Path) -> None:
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(
        workspace,
        [
            Segment(start=float(i), end=float(i) + 0.5, text=f"line {i}", source="a")
            for i in range(5)
        ],
    )

    first = read_transcript(meeting, offset=0, limit=2)
    assert (first.offset, first.returned, first.next) == (0, 2, 2)
    assert first.text == "00:00:00.000 [a] line 0\n00:00:01.000 [a] line 1"

    second = read_transcript(meeting, offset=first.next, limit=2)
    assert (second.offset, second.returned, second.next) == (2, 2, 4)
    assert second.text == "00:00:02.000 [a] line 2\n00:00:03.000 [a] line 3"

    last = read_transcript(meeting, offset=second.next, limit=2)
    assert (last.offset, last.returned, last.next) == (4, 1, None)
    assert last.text == "00:00:04.000 [a] line 4"


def test_falls_back_to_raw_segments_on_one_timeline(tmp_path: Path) -> None:
    _, meeting, workspace = _meeting(tmp_path)
    write_json(
        workspace / "segments.json",
        {
            "sources": {
                "a": [
                    to_dict(Segment(start=2.0, end=3.0, text="a2", source="a")),
                    to_dict(Segment(start=0.0, end=1.0, text="a0", source="a")),
                ],
                "b": [to_dict(Segment(start=1.0, end=2.0, text="b1", source="b"))],
            },
            "meta": {"backend": "apple"},
        },
    )

    page = read_transcript(meeting)
    assert page.source == "transcript"
    assert page.total == 3
    assert page.text == (
        "00:00:00.000 [a] a0\n00:00:01.000 [b] b1\n00:00:02.000 [a] a2"
    )


def test_no_transcript_is_actionable(tmp_path: Path) -> None:
    _, meeting, _ = _meeting(tmp_path)
    with pytest.raises(FileNotFoundError, match="no transcript"):
        read_transcript(meeting)


def test_no_workspace_is_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    with pytest.raises(FileNotFoundError, match="no workspace"):
        read_transcript(meeting)


def test_negative_limit_is_rejected(tmp_path: Path) -> None:
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(workspace, [Segment(start=0.0, end=1.0, text="x", source="a")])
    with pytest.raises(ValueError, match="limit"):
        read_transcript(meeting, limit=-1)


def test_zero_limit_is_rejected(tmp_path: Path) -> None:
    """A zero page cannot advance the cursor, so it is refused outright."""
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(workspace, [Segment(start=0.0, end=1.0, text="x", source="a")])
    with pytest.raises(ValueError, match="at least 1"):
        read_transcript(meeting, limit=0)
