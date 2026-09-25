"""Cross-talk-aware per-segment speaker attribution by relative source energy.

Close microphones (lavaliers) rarely isolate one voice: each mic picks up its
owner loudest and the neighbouring speakers at some attenuation. The per-channel
"one source == one speaker" shortcut then misattributes any segment whose
surviving transcript came from a *bleed-dominated* channel. This module chooses a
segment's speaker from the **relative, gain-normalized energy** across the
candidate sources in that segment's time window instead.

The energy comparison is deliberately gain-normalized (each source divided by its
own robust speech level), so a hot or quiet capsule does not win merely by being
hot or quiet -- only by carrying more of *that window* relative to its own normal.

Two entry points share that idea:

- :func:`attribute_segments` estimates one **static** per-source level over the
  whole recording and uses it for every segment. Synthetic ground truth shows this
  is enough when the gain imbalance between sources is constant.
- :func:`attribute_segments_windowed` estimates the level from a **causal rolling
  window** ending at each segment, so it can track a gain that **drifts** over the
  tape (a mid-tape level step). It defaults to a ~15 s window and also emits a
  per-segment **calibrated confidence** (the softmax over the normalized per-source
  margins). A single static correction ties the window on a constant imbalance; the
  window only earns its keep when the gain moves. Neither path uses a pitch/F0 cue:
  synthetic cross-talk with known truth showed a fixed F0 cue *costs* accuracy
  (0.81-0.94 vs 0.99 energy-only) even when well calibrated.

Both share the room-witness contract below and keep the incoming speaker when no
candidate is loud enough.

Recording procedure
-------------------
[REQ] Whenever per-mic sources may bleed, record a **mixed/room reference** as
well. A room mic is a neutral, speaker-independent witness: it shows that a
segment was spoken even when no identified per-source channel is loud in that
window, so attribution can keep the incoming speaker instead of guessing a
bleed source. The mixed reference is deliberately **not** a speaker candidate (it
has no single speaker identity); it is used as a presence gate. It is excluded
from the candidate set even when it is also listed in ``sources`` -- the
attribution stage passes the manifest sources and separately names the
references. See ``docs/test-corpus.md``.

**A capture can carry more than one non-speaker source**, and what it is decides
what it may do: the source's ``role`` (``clear_record.core.SOURCE_ROLES``). A
``mixed`` source is a witness and gates; an ``excluded`` source -- a microphone
nobody wore, which hears whoever is nearest rather than the room, or a duplicate
feed such as a phone memo carrying the receiver's downmix of the same mics --
neither claims a segment nor refuses one. Every reference handed to a pass
**that reads back** gates (one whose file cannot be read, or which decodes to no
frames, is skipped and raises no floor), so a claim must be one **each** witness
that heard the window can account for: a
speech carried by two rooms needs a witness in each, and a witness that never
heard the window (a dropout, another room) contributes no floor.

Supported gate range: the gate compares a candidate's gain-normalized window
level against the witness's, so it is invariant to microphone and room gain. It
is calibrated for a reference that carries every speaker at a comparable level (a
"room-eye view"), and for the common one-to-three close mics with cross-talk of
about -6 dB or weaker; under those conditions covered speakers clear the gate and
unmiked bleed does not. A reference far below the per-mic level, or much stronger
cross-talk (near 0 dB), can still misclassify -- attribution then keeps the
incoming speaker rather than invent a room identity. A reference that hears one
speaker far better than the rest is not a room-eye view and does not belong in
that role: as a witness it would refuse honest claims about the speakers it hears
quietly, which is why the vocabulary sends it to ``excluded`` rather than to
``mixed``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np

from clear_record.core import Segment, Source
from clear_record.engine.audio import ASR_SAMPLE_RATE, read_audio
from clear_record.engine.merge import source_speaker_names

__all__ = [
    "attribute_by_source",
    "attribute_segments",
    "attribute_segments_windowed",
]

# Window/level energy ratio below which a source is treated as silent in the
# segment window. A source speaking normally sits near 1.0; bleed and silence sit
# far below.
_SILENCE_RATIO = 1e-3
# When a reference is present, a candidate must carry at least this share of
# each witness's normalized level to claim a segment; otherwise a witness saw
# speech the identified channels did not, and we decline to invent a speaker. With
# several references the floor is the **highest** of them: a claim has to be one
# that every witness that heard the window can account for. The
# compared quantity is a *ratio of ratios* (candidate window/level over witness
# window/level), so it is invariant to microphone and room gain. Measured on
# make_crosstalk_scene with a mix room (gains randomized 0.3-3x, 40 seeds) in the
# **non-overlapping** regime — the generator's default overlaps utterances, where
# this separation collapses (covered min ~0.74, bleed max ~2.3), so the gate is
# scoped to non-overlapping speech: a covered speaker scores >= ~1.25 and unmiked
# bleed <= ~0.69 for the common 1-3 close mics down to -6 dB (<= ~0.94 even with
# more speakers than mics), so 1.0 sits between them and the previous 0.05 left
# unmiked speech ungated across the realistic -6..-12 dB bleed range.
_ROOM_SHARE = 1.0
_FRAME_S = 0.02

# Defaults for the causal rolling level in :func:`attribute_segments_windowed`.
# ~15 s was the synthetic sweet spot for a mid-tape gain step: long enough to
# average speech, short enough to follow the step (W=5 s lagged, W=60 s trailed).
_DEFAULT_WINDOW_S = 15.0
# Recent speaking level = high percentile of the window's frame energies (dB).
# A high percentile tracks speech rather than the silence/room-tone between it.
_DEFAULT_LEVEL_PERCENTILE = 75.0
# Softmax temperature in dB: the per-source margin is divided by this before the
# softmax that yields both the choice and its confidence. 6 dB was used in the
# synthetic experiment and is the scale at which the normalized margin becomes
# informative.
_DEFAULT_SCALE_DB = 6.0
_EPS = 1e-12


def _frame_energies(x: np.ndarray, sr: int, frame_s: float) -> np.ndarray:
    hop = max(1, int(round(frame_s * sr)))
    n = x.size // hop
    if n <= 0:
        if x.size == 0:
            return np.zeros(1, dtype=np.float64)
        return np.array([float(np.mean(np.square(x, dtype=np.float64)))])
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    return np.mean(frames**2, axis=1)


# A frame within this many dB of the source's own peak counts as *active*. The
# floor is relative to the source, so it is gain-independent; frames below it are
# silence/room tone and must not drag the source's level down.
_ACTIVE_FLOOR = 1e-3  # -30 dB relative to the source peak


def _level(x: np.ndarray, sr: int, frame_s: float) -> float:
    """Robust speech level: mean energy of the source's *active* frames.

    A plain global RMS (or the loudest-half mean) includes long silences and
    makes a sparse channel look artificially quiet, which inflates its
    gain-normalized window energy and lets bleed claim windows. Averaging only
    frames within ~30 dB of the source's own peak tracks the speech level, so a
    microphone's cross-talk ratio reflects the bleed attenuation.
    """
    energies = _frame_energies(x, sr, frame_s)
    if energies.size == 0:
        return 1e-12
    peak = float(energies.max())
    if peak <= 0.0:
        return 1e-12
    active = energies[energies >= peak * _ACTIVE_FLOOR]
    return max(float(active.mean()) if active.size else peak, 1e-12)


def _window_energy(x: np.ndarray, sr: int, start_s: float, end_s: float) -> float:
    a = max(0, int(round(start_s * sr)))
    b = min(x.size, int(round(end_s * sr)))
    if b <= a:
        return 0.0
    return float(np.mean(np.square(x[a:b], dtype=np.float64)))


def _frame_levels_db(x: np.ndarray, sr: int, frame_s: float) -> np.ndarray:
    """Per-frame energy in dB (10*log10), the track the rolling level reads."""
    return 10.0 * np.log10(np.maximum(_frame_energies(x, sr, frame_s), _EPS))


def _recent_level_db(
    levels_db: np.ndarray,
    frame_s: float,
    src_start_s: float,
    window_s: float | None,
    percentile: float,
    fallback_db: float,
) -> float:
    """Source-local speech level (dB) from the causal window before ``src_start_s``.

    Uses only frames that *precede* the segment, so the level is what the source
    has been doing recently, not what it is doing now. Falls back to the static
    whole-recording level when there is too little history (e.g. the tape's first
    segment) or when ``window_s`` is ``None``/non-positive (a single static
    correction).
    """
    if window_s is None or window_s <= 0 or levels_db.size == 0:
        return fallback_db
    end = int(round(src_start_s / frame_s))
    n_hist = max(1, int(round(window_s / frame_s)))
    lo = max(0, min(end - n_hist, levels_db.size - 1))
    hi = int(np.clip(end, lo + 1, levels_db.size))
    window = levels_db[lo:hi]
    if window.size < 2:
        return fallback_db
    return float(np.percentile(window, percentile))


def _softmax(scores: np.ndarray) -> np.ndarray:
    """Softmax over per-source normalized margins (numerically stable)."""
    if scores.size == 0:
        return scores
    z = scores - float(np.max(scores))
    e = np.exp(z)
    return e / (float(np.sum(e)) + _EPS)


def _as_references(
    mixed: Source | Sequence[Source] | None,
) -> tuple[Source, ...]:
    """The mixed references a pass was given, as the tuple it reads.

    A caller may name none (no witness), one (the field playbook's room), or
    several — a capture can carry two room microphones, and one room's witness
    cannot refuse a claim about speech in the other room. Normalizing here keeps
    every caller's form and both entry points on one contract.
    """
    if mixed is None:
        return ()
    if isinstance(mixed, Source):
        return (mixed,)
    return tuple(mixed)


def _read_candidates(
    sources: Sequence[Source],
    references: Sequence[Source],
    target_sr: int,
    frame_s: float,
) -> tuple[dict[str, tuple[np.ndarray, int]], list[tuple[str, np.ndarray, int, float]]]:
    """Read every candidate source plus the witnesses that gate them.

    A source whose id is one of the *references* is not read as a candidate — it
    is a witness. Every reference that reads back is returned with its id and its
    own robust speech level (``_level``), so the gate compares a candidate's
    window against the witness's own normal without a second read, and can read
    the witness on the timebase its offset places it on; an unreadable or empty
    one is absent, and a witness that cannot be read does not gate. Candidates
    that cannot be read are skipped, as before.
    """
    ref_ids = {ref.id for ref in references}
    loaded: dict[str, tuple[np.ndarray, int]] = {}
    for src in sources:
        if src.id in ref_ids:
            continue  # a witness, never a speaker candidate
        try:
            data, sr = read_audio(src.path, target_sr)
        except Exception:
            continue
        if data.size == 0:
            continue
        loaded[src.id] = (data, sr)

    witnesses: list[tuple[str, np.ndarray, int, float]] = []
    for ref in references:
        try:
            data, sr = read_audio(ref.path, target_sr)
        except Exception:
            continue  # a witness that cannot be read simply does not gate
        if data.size == 0:
            continue
        witnesses.append((ref.id, data, sr, _level(data, sr, frame_s)))
    return loaded, witnesses


def attribute_segments(
    segments: Sequence[Segment],
    sources: Sequence[Source],
    *,
    offsets: Mapping[str, float] | None = None,
    mixed: Source | Sequence[Source] | None = None,
    target_sr: int = ASR_SAMPLE_RATE,
    frame_s: float = _FRAME_S,
    silence_ratio: float = _SILENCE_RATIO,
    room_share: float = _ROOM_SHARE,
) -> list[Segment]:
    """Assign each segment a speaker from the highest relative-energy source.

    ``segments`` carry **source-local** times (as produced by ``transcribe``);
    ``offsets`` maps a source id to ``reference_time - source_time``, so every
    source — candidate and witness alike — is read in its own window: the
    candidate's window is where its speaker would be, the witness's where it
    would have heard that speaker. ``sources`` supplies
    the caller-provided source -> speaker identity via ``Source.label`` (a
    source with no speaker label gets a generic ``Speaker N``). With ``mixed``
    given — one reference or several — each reference's room energy gates weak
    candidate claims (see the module docstring) and any entry in ``sources`` with
    the same id is skipped as a candidate, so a reference is never emitted as a
    speaker. Segments that no candidate can claim keep their incoming speaker.
    """
    if not segments:
        return list(segments)
    offsets = dict(offsets or {})

    references = _as_references(mixed)
    loaded, witnesses = _read_candidates(sources, references, target_sr, frame_s)
    levels: dict[str, float] = {
        sid: _level(data, sr, frame_s) for sid, (data, sr) in loaded.items()
    }

    label = source_speaker_names(sources)

    out: list[Segment] = []
    for seg in segments:
        source_offset = offsets.get(seg.source, 0.0)
        ref_start = seg.start + source_offset
        ref_end = seg.end + source_offset

        best_id: str | None = None
        best_ratio = -1.0
        for sid, (data, sr) in loaded.items():
            off = offsets.get(sid, 0.0)
            energy = _window_energy(data, sr, ref_start - off, ref_end - off)
            ratio = energy / levels[sid]
            if ratio > best_ratio:
                best_ratio, best_id = ratio, sid

        floor = silence_ratio
        for ref_id, data, sr, level in witnesses:
            off = offsets.get(ref_id, 0.0)
            room_ratio = (
                _window_energy(data, sr, ref_start - off, ref_end - off) / level
            )
            floor = max(floor, room_share * room_ratio)

        if best_id is None or best_ratio < floor:
            # No identified source carries this window; keep the incoming
            # attribution rather than guessing a bleed channel.
            out.append(seg)
            continue
        out.append(replace(seg, speaker=label.get(best_id, best_id)))
    return out


def attribute_segments_windowed(
    segments: Sequence[Segment],
    sources: Sequence[Source],
    *,
    offsets: Mapping[str, float] | None = None,
    mixed: Source | Sequence[Source] | None = None,
    target_sr: int = ASR_SAMPLE_RATE,
    frame_s: float = _FRAME_S,
    silence_ratio: float = _SILENCE_RATIO,
    room_share: float = _ROOM_SHARE,
    window_s: float | None = _DEFAULT_WINDOW_S,
    level_percentile: float = _DEFAULT_LEVEL_PERCENTILE,
    scale_db: float = _DEFAULT_SCALE_DB,
    gain_normalize: bool = True,
) -> list[Segment]:
    """Attribute segments per source after normalizing each source against its
    **own recent level**, and set a calibrated confidence.

    Same inputs and mixed-reference-witness contract as
    :func:`attribute_segments` — one or several witnesses, none of them a
    speaker — but the per-source level is a **causal rolling window** ending at
    the segment rather than one static whole-recording estimate. That is what lets
    the method follow a **time-varying** gain (a mid-tape level step); on a
    constant imbalance a single static correction (``window_s=None``) collapses to
    the **same decision** (the same argmax) as :func:`attribute_segments`. It is
    not identical: this path still emits the softmax confidence, whereas
    ``attribute_segments`` preserves the segment's incoming confidence.
    ``window_s`` defaults to :data:`_DEFAULT_WINDOW_S` (~15 s).

    The decision is ``argmax`` over each source's normalized margin
    ``(window_dB - recent_level_dB) / scale_db``; the returned ``confidence`` is
    the softmax probability of that winner. The raw margin is overconfident under
    a gain imbalance (synthetic ECE ~0.28-0.35), while the normalized margin is
    less so (~0.17-0.22); softmaxing the normalized margin is the emitted
    confidence.

    ``gain_normalize=False`` is the **stateless closest-mic control** (raw window
    energy, no per-source correction) with the same softmax confidence; it exists
    so the gain-normalization effect can be measured against the naive rule.

    **No pitch/F0 cue.** Synthetic ground truth showed a fixed, well-calibrated F0
    cue still loses accuracy (0.81-0.94 vs 0.99 energy-only): calibration is not
    correctness. This function reads only per-source energy.

    **Honest limit.** The evidence is synthetic additive/delay-free cross-talk
    with known truth; real lav bleed is coloured and non-stationary, and intrinsic
    speaker-level imbalance (not a mic gain) is a regime per-source normalization
    provably cannot win. Real-tape validation still needs independent labels.

    Gated segments (no candidate clears the room/silence floor) keep their
    incoming speaker and get a ``confidence`` of 0.0, marking the decline.
    """
    if not segments:
        return list(segments)
    offsets = dict(offsets or {})

    references = _as_references(mixed)
    loaded, witnesses = _read_candidates(sources, references, target_sr, frame_s)
    levels: dict[str, float] = {}
    levels_db: dict[str, float] = {}
    tracks_db: dict[str, np.ndarray | None] = {}
    for sid, (data, sr) in loaded.items():
        level = _level(data, sr, frame_s)
        levels[sid] = level
        levels_db[sid] = 10.0 * np.log10(level)
        tracks_db[sid] = _frame_levels_db(data, sr, frame_s) if gain_normalize else None

    label = source_speaker_names(sources)

    out: list[Segment] = []
    for seg in segments:
        source_offset = offsets.get(seg.source, 0.0)
        ref_start = seg.start + source_offset
        ref_end = seg.end + source_offset

        ids = list(loaded)
        scores = np.empty(len(ids), dtype=np.float64)
        best_ratio = -1.0
        for i, sid in enumerate(ids):
            data, sr = loaded[sid]
            off = offsets.get(sid, 0.0)
            energy = _window_energy(data, sr, ref_start - off, ref_end - off)
            ratio = energy / levels[sid]
            best_ratio = max(best_ratio, ratio)
            obs_db = 10.0 * np.log10(max(energy, _EPS))
            if gain_normalize:
                level_db = _recent_level_db(
                    tracks_db[sid],
                    frame_s,
                    ref_start - off,
                    window_s,
                    level_percentile,
                    levels_db[sid],
                )
            else:
                level_db = 0.0  # stateless: raw window energy, no correction
            scores[i] = (obs_db - level_db) / max(scale_db, _EPS)

        floor = silence_ratio
        for ref_id, data, sr, level in witnesses:
            off = offsets.get(ref_id, 0.0)
            room_ratio = (
                _window_energy(data, sr, ref_start - off, ref_end - off) / level
            )
            floor = max(floor, room_share * room_ratio)

        # The presence gate deliberately keeps the *static* ratio/level (the
        # calibrated room-witness contract shared with `attribute_segments`); only
        # the speaker *choice* below uses the rolling level, which is what tracks
        # drifting gain. Mixing them here would silently re-calibrate the gate.
        if not ids or best_ratio < floor:
            # No identified source carries this window; keep the incoming
            # attribution and mark the decline rather than guess a bleed channel.
            out.append(replace(seg, confidence=0.0))
            continue

        # With a single candidate softmax is degenerate (≈1.0), even when the
        # candidate barely clears the floor. That is accepted: the gate/floor, not
        # the confidence, is what rejects a weak claim, and downstream already
        # treats `confidence == 0.0` as the decline signal.
        probs = _softmax(scores)
        k = int(np.argmax(scores))
        winner = ids[k]
        out.append(
            replace(
                seg,
                speaker=label.get(winner, winner),
                confidence=float(probs[k]),
            )
        )
    return out


def attribute_by_source(
    segments: Sequence[Segment],
    sources: Sequence[Source],
    *,
    offsets: Mapping[str, float] | None = None,
    mixed: Source | Sequence[Source] | None = None,
    window_s: float | None = None,
) -> dict[str, list[Segment]]:
    """Attribute ``segments`` and group the result by ``Segment.source``.

    Attribution owns the grouping: the input is one flat list in any order, and
    the output is keyed by each segment's own ``source`` field, so regrouping
    never depends on the order the caller flattened its per-source segments in.
    Every input segment already carries that identity; this function is the
    reason the caller does not have to reconstruct the mapping positionally.

    ``window_s is None`` uses the static :func:`attribute_segments`; a value
    selects the rolling-window :func:`attribute_segments_windowed`.
    """
    if window_s is not None:
        attributed = attribute_segments_windowed(
            segments, sources, offsets=offsets, mixed=mixed, window_s=window_s
        )
    else:
        attributed = attribute_segments(segments, sources, offsets=offsets, mixed=mixed)
    grouped: dict[str, list[Segment]] = {}
    for seg in attributed:
        grouped.setdefault(seg.source, []).append(seg)
    return grouped
