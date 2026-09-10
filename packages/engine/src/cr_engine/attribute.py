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

Recording procedure
-------------------
[REQ] Whenever per-mic sources may bleed, record a **mixed/room reference** as
well. A room mic is a neutral, speaker-independent witness: it shows that a
segment was spoken even when no identified per-source channel is loud in that
window, so attribution can keep the incoming speaker instead of guessing a
bleed source. The mixed reference is deliberately **not** a speaker candidate (it
has no single speaker identity); it is used as a presence gate. See
``docs/test-corpus.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np

from cr_core import Segment, Source
from cr_engine.audio import ASR_SAMPLE_RATE, read_audio

__all__ = ["attribute_segments"]

# Window/level energy ratio below which a source is treated as silent in the
# segment window. A source speaking normally sits near 1.0; bleed and silence sit
# far below.
_SILENCE_RATIO = 1e-3
# When a room reference is present, a candidate must carry at least this share of
# the room's normalized level to claim a segment; otherwise the room saw speech
# the identified channels did not, and we decline to invent a speaker.
_ROOM_SHARE = 0.05
_FRAME_S = 0.02


def _frame_energies(x: np.ndarray, sr: int, frame_s: float) -> np.ndarray:
    hop = max(1, int(round(frame_s * sr)))
    n = x.size // hop
    if n <= 0:
        if x.size == 0:
            return np.zeros(1, dtype=np.float64)
        return np.array([float(np.mean(np.square(x, dtype=np.float64)))])
    frames = x[: n * hop].reshape(n, hop).astype(np.float64)
    return np.mean(frames**2, axis=1)


def _level(x: np.ndarray, sr: int, frame_s: float) -> float:
    """Robust speech level: mean energy of the loudest half of the frames.

    A plain global RMS would include long silences and make a mostly-silent
    channel look artificially quiet; the loudest-half level tracks the source's
    speech level instead.
    """
    energies = _frame_energies(x, sr, frame_s)
    if energies.size == 0:
        return 1e-12
    threshold = float(np.percentile(energies, 50.0))
    loud = energies[energies >= threshold]
    level = float(loud.mean()) if loud.size else float(energies.mean())
    return max(level, 1e-12)


def _window_energy(x: np.ndarray, sr: int, start_s: float, end_s: float) -> float:
    a = max(0, int(round(start_s * sr)))
    b = min(x.size, int(round(end_s * sr)))
    if b <= a:
        return 0.0
    return float(np.mean(np.square(x[a:b], dtype=np.float64)))


def attribute_segments(
    segments: Sequence[Segment],
    sources: Sequence[Source],
    *,
    offsets: Mapping[str, float] | None = None,
    mixed: Source | None = None,
    target_sr: int = ASR_SAMPLE_RATE,
    frame_s: float = _FRAME_S,
    silence_ratio: float = _SILENCE_RATIO,
    room_share: float = _ROOM_SHARE,
) -> list[Segment]:
    """Assign each segment a speaker from the highest relative-energy source.

    ``segments`` carry **source-local** times (as produced by ``transcribe``);
    ``offsets`` maps a source id to ``reference_time - source_time`` so every
    candidate can be read in the segment's reference window. ``sources`` supplies
    the caller-provided source -> speaker identity via ``Source.label`` (falling
    back to the id). With ``mixed`` given, its room energy gates weak candidate
    claims (see the module docstring). Segments that no candidate can claim keep
    their incoming speaker.
    """
    if not segments:
        return list(segments)
    offsets = dict(offsets or {})

    loaded: dict[str, tuple[np.ndarray, int]] = {}
    levels: dict[str, float] = {}
    for src in sources:
        try:
            data, sr = read_audio(src.path, target_sr)
        except Exception:
            continue
        if data.size == 0:
            continue
        loaded[src.id] = (data, sr)
        levels[src.id] = _level(data, sr, frame_s)

    mixed_data: np.ndarray | None = None
    mixed_sr = target_sr
    mixed_level = 1e-12
    if mixed is not None:
        try:
            data, sr = read_audio(mixed.path, target_sr)
            if data.size:
                mixed_data, mixed_sr = data, sr
                mixed_level = _level(data, sr, frame_s)
        except Exception:
            pass  # an unreadable room reference simply does not gate

    label = {src.id: (src.label or src.id) for src in sources}

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
        if mixed_data is not None:
            room_ratio = (
                _window_energy(mixed_data, mixed_sr, ref_start, ref_end) / mixed_level
            )
            floor = max(floor, room_share * room_ratio)

        if best_id is None or best_ratio < floor:
            # No identified source carries this window; keep the incoming
            # attribution rather than guessing a bleed channel.
            out.append(seg)
            continue
        out.append(replace(seg, speaker=label.get(best_id, best_id)))
    return out
