"""Reading a meeting's transcript: the text an agent reasons about.

The transcript is workspace files, not registry state, so these exercise the
service reader directly (temp workspace, no run, no backend): reconciled record
preferred, raw segments fallback, and the slice cursor for a long tape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.core import RecordDocument, Segment, to_dict, write_json
from clear_record.pipeline.workspace import Workspace, publish_run
from clear_record.service import Registry, read_transcript


def _registry(tmp_path: Path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.create_project(
        "Ops",
        actor="console",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor="console",
    )
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


def test_a_pre_roll_reads_as_a_negative_time(tmp_path: Path) -> None:
    """The text reads the record's own clock, sign and all.

    A source started before the reference — the phone first, the ordinary case —
    has negative reference times, and the record page must say so: clamping them
    to `00:00:00.000` is the same fold the SRT/VTT renderers dropped (their cues
    came out zero-length) and would here name the wrong instant for the tape's
    first cues. This page is the reconciled record's, so its clock is the
    reference one — unlike the raw fallback, pinned below.
    """
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(
        workspace,
        [
            Segment(start=-114.365, end=-112.0, text="before", source="phone"),
            Segment(start=1.0, end=3.5, text="after", source="room"),
        ],
    )

    page = read_transcript(meeting)

    assert page.source == "record"
    assert page.text == "-00:01:54.365 [phone] before\n00:00:01.000 [room] after"


def test_the_raw_page_keeps_each_source_own_clock(tmp_path: Path) -> None:
    """The fallback page is source-local, not the reference clock.

    `reconcile` is what shifts a source onto the reference, so a workspace read
    before it — or without a record — holds each source's own times: the same
    cue reads ``00:00:04.000`` on the raw page and ``00:00:01.500`` on the
    record, whose alignment moved it by -2.5 s. Both are read as the page holds
    them, which is why the module names the page's clock rather than one clock.
    """
    _, meeting, workspace = _meeting(tmp_path)
    raw_segment = Segment(start=4.0, end=5.0, text="hello", source="b")
    write_json(
        workspace / "segments.json",
        {"sources": {"b": [to_dict(raw_segment)]}, "meta": {"backend": "apple"}},
    )

    raw = read_transcript(meeting)
    assert raw.source == "transcript"
    assert raw.text == "00:00:04.000 [b] hello"

    _write_record(workspace, [Segment(start=1.5, end=2.5, text="hello", source="b")])

    record = read_transcript(meeting)
    assert record.source == "record"
    assert record.text == "00:00:01.500 [b] hello"


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
    registry.create_project(
        "Ops",
        actor="console",
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )
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


# --- the run the page came from (ADR-0033) ---------------------------------- #
def _published_record(workspace: Path, run_id: int, text: str) -> None:
    """Publish one finished run's record, as the run path leaves it."""
    scope = Workspace.at(workspace).run_scope(run_id)
    scope.write_record(
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(Segment(start=0.0, end=1.5, text=text, source="a"),),
        )
    )
    publish_run(scope)


def test_a_transcript_names_the_run_that_produced_it(tmp_path: Path) -> None:
    """A reader can say which run's transcript this is.

    The documents a run writes name it, so the page carries the name too — and
    the run's own, unrewritten copy stays readable beside the published one.
    """
    _, meeting, workspace = _meeting(tmp_path)
    _published_record(workspace, 7, "hello")

    page = read_transcript(meeting)

    assert page.run_id == 7
    assert page.path == str(workspace / "record.json")
    assert page.text == "00:00:00.000 [a] hello"
    assert Workspace.at(workspace).run_scope(7).record_path.read_text(
        encoding="utf-8"
    ) == (workspace / "record.json").read_text(encoding="utf-8")


def test_the_newest_run_is_the_default_read(tmp_path: Path) -> None:
    """A re-run publishes its own copy; the workspace read follows it."""
    _, meeting, workspace = _meeting(tmp_path)
    _published_record(workspace, 1, "first")
    assert read_transcript(meeting).text == "00:00:00.000 [a] first"

    _published_record(workspace, 2, "second")
    page = read_transcript(meeting)

    assert page.run_id == 2
    assert page.text == "00:00:00.000 [a] second"


def test_a_transcript_written_without_a_run_names_none(tmp_path: Path) -> None:
    """A transcript written outside a run — a stage command's, ``calibrate``'s —
    names no run id, and reads as none."""
    _, meeting, workspace = _meeting(tmp_path)
    _write_record(workspace, [Segment(start=0.0, end=1.0, text="hello", source="a")])
    assert read_transcript(meeting).run_id is None


def test_a_hand_edited_run_id_is_no_run(tmp_path: Path) -> None:
    """Only an ``int`` names a run: a string or a ``bool`` is a hand-edit, not one."""
    _, meeting, workspace = _meeting(tmp_path)
    write_json(
        workspace / "segments.json",
        {
            "sources": {
                "a": [to_dict(Segment(start=0.0, end=1.0, text="hi", source="a"))]
            },
            "meta": {"run_id": "7"},
        },
    )
    assert read_transcript(meeting).run_id is None
