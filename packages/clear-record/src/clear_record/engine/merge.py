"""Reconcile per-source ASR segments into one attributed timeline.

This implements the v1 *reconcile* step. Segments arrive source-local (in each
recording's own clock). We shift them onto the reference timebase via the
alignment offsets, then collapse the overlapping set into a single, attributable
timeline. Because each source is its own recorded channel, **speaker ~= source**
(the record's whole point: with dedicated channels, diarization nearly
disappears as an ML problem). ``source.label`` (when present) is used as the
speaker name; a source with no speaker label gets a generic ``Speaker N`` so a
tape's file name never becomes a person in the minutes.

A source the alignment could not place (``align`` writes it into
``Alignment.unresolved`` and gives it **no** offset) has no honest position on
the reference clock, so its segments are left out of the timeline entirely —
never read as a zero offset and merged in at the reference's start. The
alignment's ``unresolved`` list already named the sources themselves;
:func:`unplaced_sources` adds what it did not say — how much transcript went with
each one that had segments — and the record carries that in
``metadata["unplaced"]``, beside the segments they did not contribute to.
"""

from __future__ import annotations

from collections.abc import Sequence

from clear_record.core import Alignment, Segment, Source, UnplacedSource
from clear_record.engine.text import clean_segments

# The longest pause between two same-speaker cues that is still bridged. Only a
# *positive* gap (a real pause) joins: ASR emits distinct sentences as touching
# cues (``end == start``), and merging those produced multi-sentence 25-30 s
# cues that were useless as subtitles.
_JOIN_GAP_S = 0.5
# Maximum length of a single cue; a longer continuous run is split so SRT/VTT
# cues stay playable.
_MAX_CUE_S = 30.0


def _priority(seg: Segment, order: dict[str, int]) -> tuple[float, int]:
    """Higher is better. Confidence first; stable source-order tie-break."""
    return (seg.confidence or 0.0), -order.get(seg.source, 0)


def _overlaps(a: Segment, b: Segment) -> bool:
    """True when ``a`` and ``b`` strictly overlap (touching endpoints do not)."""
    return a.start < b.end - 1e-9 and b.start < a.end - 1e-9


def _weighted_conf(a: Segment, b: Segment) -> float | None:
    ca, cb = a.confidence, b.confidence
    if ca is None and cb is None:
        return None
    ca = ca or 0.0
    cb = cb or 0.0
    return round((ca + cb) / 2.0, 4)


def source_speaker_names(sources: Sequence[Source]) -> dict[str, str]:
    """Map each source id to the speaker name its segments carry.

    A caller-provided ``label`` wins. A source with no label -- or a label that
    is just its id, which an older ``ingest`` copied from the file name -- is
    not a person: it gets a stable anonymous ``Speaker N``. Minutes and exports
    then never present a tape's file name as an attendee. An anonymous name is
    allocated around any explicit label already in use, so ``Speaker 1`` beside
    an unlabelled source cannot name two different people. The reservation is
    case-insensitive: an explicit ``speaker 1`` blocks the anonymous form too.
    """
    names: dict[str, str] = {}
    used: set[str] = set()
    anonymous: list[Source] = []
    for src in sources:
        label = (src.label or "").strip()
        if label and label != src.id:
            names[src.id] = label
            used.add(label.casefold())
        else:
            anonymous.append(src)
    next_n = 0
    for src in anonymous:
        next_n += 1
        while f"Speaker {next_n}".casefold() in used:
            next_n += 1
        name = f"Speaker {next_n}"
        names[src.id] = name
        used.add(name.casefold())
    return names


def _placements(
    alignment: Alignment | None, sources: Sequence[Source]
) -> dict[str, float]:
    """The offsets reconcile may place segments on, keyed by source id.

    A source is placeable only when the alignment carries a real offset for it: a
    missing entry is the align stage's deliberate silence — the id it recorded in
    ``Alignment.unresolved`` — not a position on the reference clock, and neither
    is an entry whose value is **null** (a hand-written or foreign manifest can
    carry one). Both read as "no position" here, because this one map is read by
    membership (:func:`unplaced_sources`) and by lookup (:func:`reconcile`), and
    a source cannot be dropped by one and unnamed by the other. The reference is
    the one exception and always sits at zero, because it *is* the record's
    timeline; with no alignment at all the first source is the reference
    ``align_sources`` would have chosen, and the only placeable one.

    Reading a missing entry as ``0.0`` is the bug this closes: it stacked a tape
    that could not be placed on the reference's zero point, where the artifact
    could not tell it from a source that really starts there.
    """
    reference = alignment.reference if alignment else (sources[0].id if sources else "")
    offsets = (
        {sid: off for sid, off in alignment.offsets.items() if off is not None}
        if alignment
        else {}
    )
    if reference:
        offsets.setdefault(reference, 0.0)
    return offsets


def unplaced_sources(
    per_source: dict[str, list[Segment]],
    alignment: Alignment | None,
    sources: list[Source],
) -> tuple[UnplacedSource, ...]:
    """What :func:`reconcile` leaves out for want of an offset, and how much.

    One entry per source that holds segments but no real alignment offset — the
    sources ``align`` recorded in ``Alignment.unresolved``, plus any a
    hand-written manifest left *present but null*. It reads the same placement
    rule :func:`reconcile` does (``_placements``), so a caller can write the
    record's drop summary without reconciling twice and a source dropped by one
    is always named by the other. A source with no segments drops nothing and is
    not listed: it is named by the alignment, not here — and by nothing at all when
    the manifest carries no alignment.
    """
    offsets = _placements(alignment, sources)
    out: list[UnplacedSource] = []
    for src in sources:
        if src.id in offsets:
            continue
        segs = per_source.get(src.id, ())
        if not segs:
            continue
        out.append(
            UnplacedSource(
                id=src.id,
                segments=len(segs),
                speech_s=round(sum(max(0.0, s.end - s.start) for s in segs), 4),
            )
        )
    return tuple(out)


def _join_continuous(segments: Sequence[Segment], max_cue_s: float) -> list[Segment]:
    """Bridge a same-source/speaker pair separated by a real pause, never
    letting a joined cue exceed ``max_cue_s``.

    Touching segments (``seg.start <= cur.end``) are *not* joined: the decoder
    deliberately ended a sentence there, and joining them merged distinct
    sentence cues into one 25-30 s block.
    """
    out: list[Segment] = []
    cur: Segment | None = None
    for seg in segments:
        if (
            cur is not None
            and seg.source == cur.source
            and (seg.speaker or "") == (cur.speaker or "")
            and seg.start > cur.end + 1e-9
            and seg.start - cur.end <= _JOIN_GAP_S
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
    """Greedily keep a maximal non-overlapping set, ordered by priority.

    Selection is **non-transitive**: a segment is dropped only when it overlaps
    a segment we actually *kept*, never merely because it overlaps some other
    dropped segment. Iterating in descending ``_priority`` keeps exactly one
    survivor from a set of mutually-overlapping duplicates (N -> 1) while
    preserving a distinct, non-overlapping event that merely bridges two
    duplicates. The previous connected-component loop absorbed such a bridge
    into the cluster and then discarded it, silently losing a real utterance.
    """
    kept: list[Segment] = []
    for seg in sorted(segments, key=lambda s: _priority(s, order), reverse=True):
        if all(not _overlaps(seg, k) for k in kept):
            kept.append(seg)
    kept.sort(key=lambda s: (s.start, s.end))
    return kept


def reconcile(
    per_source: dict[str, list[Segment]],
    alignment: Alignment | None,
    sources: list[Source],
    max_cue_s: float = _MAX_CUE_S,
) -> list[Segment]:
    """Produce a reconciled, source-attributed segment list on the reference
    timebase. The reference source keeps its time; every other source is shifted
    by its alignment offset.

    A source with **no** offset — one ``align`` left unresolved — has no honest
    position on the reference clock, so its segments are left out rather than
    stacked on the reference's zero point; :func:`unplaced_sources` names what
    that dropped, and the record carries it. With no alignment at all, the first
    source is the reference and the only placeable one."""
    order = {s.id: i for i, s in enumerate(sources)}
    label = source_speaker_names(sources)
    offsets = _placements(alignment, sources)

    shifted: list[Segment] = []
    for src in sources:
        off = offsets.get(src.id)
        if off is None:
            continue  # unplaceable: no offset, so no position on the reference
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


__all__ = ["reconcile", "source_speaker_names", "unplaced_sources"]
