"""Read a meeting's transcript as text, with paging for long tapes.

A meeting's transcript lives as files in its workspace: ``record.json`` (the
reconciled, attributed record) and ``segments.json`` (the raw per-source ASR
segments). The run's artifacts point into the run's **own copy** of those files
(``<workspace>/runs/<run id>/``, ADR-0033), not at the workspace root this module
reads, but an agent driving the glossary ↔ transcript tuning loop needs the
**text** it is meant to reason about, not a path. This module owns that read (and
its slice) so the MCP adapter stays argument marshalling.

The reconciled record wins when it has segments; otherwise the raw segments are
read as they are written, each source's times in its own clock — the page lists
them in start order across those clocks, and `reconcile` is what shifts them onto
one. Reading is tolerant of a not-yet-reconciled workspace, and a ranged read
keeps a multi-hour tape from arriving whole.

The documents read are the workspace's **published** copy: the newest finished
run published its own copy there, so this is the default read. Each document
names the run that wrote it (``run_id`` in the record's metadata and in the
segments' meta, ADR-0033) and the page carries that name, so a reader can say
which run produced the transcript in hand — and that run's own, unrewritten copy
stays readable at ``<workspace>/runs/<run id>/``.
"""

from __future__ import annotations

import dataclasses

from clear_record.pipeline.stages import format_timestamp
from clear_record.pipeline.workspace import Workspace
from clear_record.core import (
    Segment,
    load_json,
    record_from_dict,
    segment_from_dict,
)
from clear_record.service.models import Meeting


def _render(segments: list[Segment]) -> str:
    """One ``HH:MM:SS.mmm [speaker] text`` line per segment.

    A time is read as the page holds it, sign and all: no pre-roll is silently
    clamped to a zero-length span, which is the clamp that made the export's
    SRT/VTT cues zero-length and its Markdown header a bare
    ``[00:00:00.000–00:00:00.000]``.
    The rounding `format_timestamp` applies is to the millisecond the time prints
    as: the last half-millisecond before zero reads ``00:00:00.000`` rather than a
    signed ``-00:00:00.000``. Which clock the page holds depends on it:
    ``read_transcript`` renders the reconciled record on the reference clock, where
    a source started before the reference reads ``-00:01:54.365``, or a
    not-yet-reconciled workspace's raw per-source segments, whose times are each
    source's own — `reconcile` is what shifts them onto the reference.
    """
    return "\n".join(
        f"{format_timestamp(seg.start)} [{seg.speaker or seg.source}] {seg.text}"
        for seg in segments
    )


@dataclasses.dataclass(frozen=True)
class TranscriptSlice:
    """One page of a meeting's transcript text plus the cursor to continue.

    ``source`` is ``"record"`` when the reconciled record was read and
    ``"transcript"`` for the raw per-source segments. ``next`` is the offset to
    pass for the following page, or ``None`` at the end; ``text`` is the
    rendered page (one ``HH:MM:SS.mmm [speaker] text`` line per segment). Each
    time is read as its page holds it, sign included, with no pre-roll silently
    clamped to a zero-length span: the reconciled record's reference clock (a
    source started before the reference reads ``-00:01:54.365``), or the raw
    fallback's per-source clocks, which `reconcile` is what shifts onto the
    reference.

    ``run_id`` is the run that produced the page: the documents a run writes
    name it (``Workspace.write_record``/``write_segments``, ADR-0033), so a
    reader — or an agent that tuned the glossary and is reading the result — can
    say which run's transcript this is, and fetch that run's own copy rather
    than the workspace's published one. The unscoped writers name none: the
    stage commands and ``calibrate`` write at the workspace root, and a
    transcript written before runs were scoped has no name to give — such a page
    carries ``None``.
    """

    meeting_id: int
    path: str
    source: str
    total: int
    offset: int
    returned: int
    next: int | None
    text: str
    run_id: int | None = None


def _run_id(meta: dict) -> int | None:
    """The run that wrote a document, from the meta it stamps itself with.

    An ``int``, never a bool, and never below 1: run ids start at 1
    (:func:`~clear_record.pipeline.workspace._run_scope` reads a marker's run id
    by the same rule), and a hand-edited body can hold anything — a value that is
    not a run id is no run id.
    """
    value = (meta or {}).get("run_id")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _reconciled(w: Workspace) -> tuple[list[Segment], dict]:
    if not w.record_path.is_file():
        return [], {}
    record = record_from_dict(load_json(w.record_path))
    return list(record.segments), dict(record.metadata)


def _segments(w: Workspace) -> tuple[list[Segment], dict]:
    if not w.segments_path.is_file():
        return [], {}
    data = load_json(w.segments_path)
    per_source = data.get("sources", {})
    merged = [segment_from_dict(raw) for segs in per_source.values() for raw in segs]
    merged.sort(key=lambda seg: (seg.start, seg.source))
    return merged, dict(data.get("meta", {}))


def read_transcript(
    meeting: Meeting,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> TranscriptSlice:
    """Read a meeting's transcript text, optionally sliced by segment index.

    ``offset`` skips that many segments and ``limit`` caps the page (at least 1 when
    given); both are the natural paging cursor an agent uses on a long tape. A reconciled record
    with segments is preferred; otherwise the raw per-source segments are read
    as they are written, each source's times in its own clock; `reconcile` is
    what shifts them onto one. Raises :class:`FileNotFoundError` when the
    workspace has neither file (no run has produced a transcript yet).
    """
    if not meeting.workspace_path:
        raise FileNotFoundError(
            f"meeting {meeting.slug!r} has no workspace; set one before reading"
        )
    w = Workspace.at(meeting.workspace_path)
    candidates = [
        (*_reconciled(w), "record", w.record_path),
        (*_segments(w), "transcript", w.segments_path),
    ]
    chosen = next((c for c in candidates if c[0]), None)
    if chosen is None and any(path.is_file() for _, _, _, path in candidates):
        chosen = candidates[0] if w.record_path.is_file() else candidates[1]
    if chosen is None:
        raise FileNotFoundError(
            f"no transcript for {meeting.project_slug}/{meeting.slug}; "
            f"expected {w.record_path} or {w.segments_path}"
        )
    segments, meta, source, path = chosen

    total = len(segments)
    start = max(0, offset)
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    end = total if limit is None else start + limit
    page = segments[start:end]
    following = start + len(page)
    return TranscriptSlice(
        meeting_id=meeting.id,
        path=str(path),
        source=source,
        total=total,
        offset=start,
        returned=len(page),
        next=following if following < total else None,
        text=_render(page),
        run_id=_run_id(meta),
    )


__all__ = ["TranscriptSlice", "read_transcript"]
