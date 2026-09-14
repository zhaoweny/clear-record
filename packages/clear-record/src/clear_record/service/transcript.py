"""Read a meeting's transcript as text, with paging for long tapes.

A meeting's transcript lives as files in its workspace: ``record.json`` (the
reconciled, attributed record) and ``segments.json`` (the raw per-source ASR
segments). The run's artifacts point at those files, but an agent driving the
glossary ↔ transcript tuning loop needs the **text** it is meant to reason
about, not a path. This module owns that read (and its slice) so the MCP adapter
stays argument marshalling.

The reconciled record wins when it has segments; otherwise the raw segments are
read and merged onto one timeline. Reading is tolerant of a not-yet-reconciled
workspace, and a ranged read keeps a multi-hour tape from arriving whole.
"""

from __future__ import annotations

import dataclasses

from clear_record.cli.workspace import Workspace
from clear_record.core import (
    Segment,
    load_json,
    record_from_dict,
    segment_from_dict,
)
from clear_record.service.models import Meeting


def _fmt_ts(seconds: float) -> str:
    """``HH:MM:SS.mmm`` for a segment start, matching the console's preview."""
    m, s = divmod(max(0.0, float(seconds)), 60.0)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _render(segments: list[Segment]) -> str:
    return "\n".join(
        f"{_fmt_ts(seg.start)} [{seg.speaker or seg.source}] {seg.text}"
        for seg in segments
    )


@dataclasses.dataclass(frozen=True)
class TranscriptSlice:
    """One page of a meeting's transcript text plus the cursor to continue.

    ``source`` is ``"record"`` when the reconciled record was read and
    ``"transcript"`` for the raw per-source segments. ``next`` is the offset to
    pass for the following page, or ``None`` at the end; ``text`` is the
    rendered page (one ``HH:MM:SS.mmm [speaker] text`` line per segment).
    """

    meeting_id: int
    path: str
    source: str
    total: int
    offset: int
    returned: int
    next: int | None
    text: str


def _reconciled(w: Workspace) -> list[Segment]:
    if not w.record_path.is_file():
        return []
    return list(record_from_dict(load_json(w.record_path)).segments)


def _segments(w: Workspace) -> list[Segment]:
    if not w.segments_path.is_file():
        return []
    per_source = load_json(w.segments_path).get("sources", {})
    merged = [segment_from_dict(raw) for segs in per_source.values() for raw in segs]
    merged.sort(key=lambda seg: (seg.start, seg.source))
    return merged


def read_transcript(
    meeting: Meeting,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> TranscriptSlice:
    """Read a meeting's transcript text, optionally sliced by segment index.

    ``offset`` skips that many segments and ``limit`` caps the page; both are
    the natural paging cursor an agent uses on a long tape. A reconciled record
    with segments is preferred; otherwise the raw per-source segments are
    merged onto one timeline. Raises :class:`FileNotFoundError` when the
    workspace has neither file (no run has produced a transcript yet).
    """
    if not meeting.workspace_path:
        raise FileNotFoundError(
            f"meeting {meeting.slug!r} has no workspace; set one before reading"
        )
    w = Workspace.at(meeting.workspace_path)
    candidates = [
        (_reconciled(w), "record", w.record_path),
        (_segments(w), "transcript", w.segments_path),
    ]
    chosen = next((c for c in candidates if c[0]), None)
    if chosen is None and any(path.is_file() for _, _, path in candidates):
        chosen = candidates[0] if w.record_path.is_file() else candidates[1]
    if chosen is None:
        raise FileNotFoundError(
            f"no transcript for {meeting.project_slug}/{meeting.slug}; "
            f"expected {w.record_path} or {w.segments_path}"
        )
    segments, source, path = chosen

    total = len(segments)
    start = max(0, offset)
    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
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
    )


__all__ = ["TranscriptSlice", "read_transcript"]
