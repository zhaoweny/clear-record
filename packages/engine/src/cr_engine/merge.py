"""Reconcile per-source ASR segments into one attributed timeline.

This implements the v1 *reconcile* step. Segments arrive source-local (in each
recording's own clock). We shift them onto the reference timebase via the
alignment offsets, then collapse the overlapping set into a single, attributable
timeline. Because each source is its own recorded channel, **speaker ~= source**
(the record's whole point: with dedicated channels, diarization nearly
disappears as an ML problem). ``source.label`` (when present) is used as the
speaker name.
"""

from __future__ import annotations

from collections.abc import Sequence

from cr_core import Alignment, Segment, Source
from cr_engine.text import clean_segments

# Treat same-source segments within this gap as continuous.
_JOIN_GAP_S = 0.5
# Maximum length of a single cue; a longer continuous run is split so SRT/VTT
# cues stay playable.
_MAX_CUE_S = 30.0


def _priority(seg: Segment, order: dict[str, int]) -> tuple[float, int]:
    """Higher is better. Confidence first; stable source-order tie-break."""
    return (seg.confidence or 0.0), -order.get(seg.source, 0)


def _weighted_conf(a: Segment, b: Segment) -> float | None:
    ca, cb = a.confidence, b.confidence
    if ca is None and cb is None:
        return None
    ca = ca or 0.0
    cb = cb or 0.0
    return round((ca + cb) / 2.0, 4)


def _join_continuous(segments: Sequence[Segment], max_cue_s: float) -> list[Segment]:
    """Join same-source/speaker segments separated by <= the join gap, never
    letting a joined cue exceed ``max_cue_s``."""
    out: list[Segment] = []
    cur: Segment | None = None
    for seg in segments:
        if (
            cur is not None
            and seg.source == cur.source
            and (seg.speaker or "") == (cur.speaker or "")
            and seg.start <= cur.end + _JOIN_GAP_S
            and (max(cur.end, seg.end) - cur.start) <= max_cue_s
        ):
            cur = Segment(
                start=cur.start,
                end=max(cur.end, seg.end),
                text=(cur.text + " " + seg.text).strip(),
                source=cur.source,
                speaker=cur.speaker,
                confidence=_weighted_conf(cur, seg),
                language=seg.language or cur.language,
            )
            continue
        if cur is not None:
            out.append(cur)
        cur = seg
    if cur is not None:
        out.append(cur)
    return out


def _resolve_overlaps(
    segments: Sequence[Segment], order: dict[str, int]
) -> list[Segment]:
    """Keep one survivor per connected component of mutually overlapping
    segments (deterministic: confidence, then source order).

    Resolving pairwise left ~ceil(N/2) duplicates when three or more sources
    overlapped the same moment.
    """
    out: list[Segment] = []
    cluster: list[Segment] = []
    cluster_end = 0.0
    for seg in segments:
        if cluster and seg.start < cluster_end - 1e-9:
            cluster.append(seg)
            cluster_end = max(cluster_end, seg.end)
            continue
        if cluster:
            out.append(max(cluster, key=lambda s: _priority(s, order)))
        cluster = [seg]
        cluster_end = seg.end
    if cluster:
        out.append(max(cluster, key=lambda s: _priority(s, order)))
    return out


def reconcile(
    per_source: dict[str, list[Segment]],
    alignment: Alignment | None,
    sources: list[Source],
    max_cue_s: float = _MAX_CUE_S,
) -> list[Segment]:
    """Produce a reconciled, source-attributed segment list on the reference
    timebase. The reference source keeps its time; other sources are shifted by
    their alignment offset (default 0 when unaligned or for the reference)."""
    order = {s.id: i for i, s in enumerate(sources)}
    label = {s.id: (s.label or s.id) for s in sources}

    shifted: list[Segment] = []
    for src in sources:
        off = alignment.offsets.get(src.id, 0.0) if alignment else 0.0
        for seg in per_source.get(src.id, ()):
            shifted.append(
                Segment(
                    start=round(seg.start + off, 4),
                    end=round(seg.end + off, 4),
                    text=seg.text.strip(),
                    source=src.id,
                    # Preserve a diarized speaker if one was assigned; otherwise
                    # fall back to the source's label (per-channel case).
                    speaker=seg.speaker or label[src.id],
                    confidence=seg.confidence,
                    language=seg.language,
                )
            )

    shifted = clean_segments(shifted)
    shifted.sort(key=lambda s: (s.start, s.end))
    joined = _join_continuous(shifted, max_cue_s)
    resolved = _resolve_overlaps(joined, order)
    resolved.sort(key=lambda s: (s.start, s.end))
    return [s for s in resolved if s.text.strip()]


__all__ = ["reconcile"]
