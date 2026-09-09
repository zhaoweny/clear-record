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

from cr_core import Alignment, Segment, Source

# Treat same-source segments within this gap as continuous.
_JOIN_GAP_S = 0.5
# Overlap ratio above which a later segment may *replace* an earlier one.
_REPLACE_OVERLAP = 0.6


def _priority(seg: Segment, order: dict[str, int]) -> tuple[float, int]:
    """Higher is better. Confidence first; stable source-order tie-break."""
    return (seg.confidence or 0.0), -order.get(seg.source, 0)


def reconcile(
    per_source: dict[str, list[Segment]],
    alignment: Alignment | None,
    sources: list[Source],
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
                    speaker=label[src.id],
                    confidence=seg.confidence,
                    language=seg.language,
                )
            )

    shifted.sort(key=lambda s: (s.start, s.end))

    out: list[Segment] = []
    cur: Segment | None = None
    for seg in shifted:
        if not seg.text:
            continue
        if cur is None:
            cur = seg
            continue
        if seg.source == cur.source and seg.start <= cur.end + _JOIN_GAP_S:
            # same speaker, continuous -> join
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
        if seg.start < cur.end:
            # different sources overlap -> keep the better one
            if _priority(seg, order) > _priority(cur, order):
                out.append(seg)
            else:
                out.append(cur)
            cur = None
            continue
        out.append(cur)
        cur = seg

    if cur is not None:
        out.append(cur)

    # Re-sort by start (replacements can reorder) and drop empty.
    out.sort(key=lambda s: (s.start, s.end))
    return [s for s in out if s.text.strip()]


def _weighted_conf(a: Segment, b: Segment) -> float | None:
    ca, cb = a.confidence, b.confidence
    if ca is None and cb is None:
        return None
    ca = ca or 0.0
    cb = cb or 0.0
    return round((ca + cb) / 2.0, 4)


__all__ = ["reconcile"]
