"""Chunking for long recordings.

Long meeting tapes are transcribed in fixed overlapping windows so that (a) the
work is checkpointed and resumable, and (b) each unit of work is bounded. This
module is pure timeline math + WAV slicing; orchestration and caching live in the
CLI stage.
"""

from __future__ import annotations

from pathlib import Path

import soundfile as sf

# Single owner: ``core`` defines the run-option defaults and ``engine`` re-exports
# them here (``engine → core`` is an allowed edge), so importers of either keep
# working and there is no second literal to drift.
from clear_record.core import DEFAULT_CHUNK_S, DEFAULT_OVERLAP_S


def plan_chunks(
    duration_s: float,
    chunk_s: float = DEFAULT_CHUNK_S,
    overlap_s: float = DEFAULT_OVERLAP_S,
) -> list[tuple[float, float]]:
    """Split ``[0, duration_s]`` into overlapping ``(start, end)`` windows.

    A recording shorter than one chunk is a single window. Consecutive windows
    overlap by ``overlap_s`` so words at a boundary are fully present in at least
    one chunk; the caller de-duplicates the overlap when merging.
    """
    if duration_s <= 0:
        return []
    if chunk_s <= 0 or duration_s <= chunk_s:
        return [(0.0, float(duration_s))]
    if overlap_s < 0:
        raise ValueError(f"overlap_s must be >= 0 (got {overlap_s})")
    if overlap_s >= chunk_s:
        # Otherwise ``start = end - overlap`` never advances and planning loops
        # forever (or walks backwards). Reject rather than emit identical windows.
        raise ValueError(
            f"overlap_s ({overlap_s}) must be smaller than chunk_s ({chunk_s}) "
            "or chunk planning cannot make forward progress"
        )
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_s:
        end = min(float(duration_s), start + chunk_s)
        chunks.append((start, end))
        if end >= duration_s:
            break
        start = max(0.0, end - overlap_s)
    return chunks


def write_chunk(
    src: str | Path,
    dst: str | Path,
    start_s: float,
    end_s: float,
    *,
    sr: int = 16000,
) -> float:
    """Write ``src[start_s:end_s]`` to a 16 kHz mono WAV ``dst``.

    Returns the chunk's actual duration in seconds.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    start = max(0, int(round(start_s * sr)))
    frames = max(0, int(round((end_s - start_s) * sr)))
    data, file_sr = sf.read(
        str(src), start=start, frames=frames, dtype="float32", always_2d=False
    )
    sf.write(str(dst), data, file_sr)
    return len(data) / file_sr


__all__ = ["DEFAULT_CHUNK_S", "DEFAULT_OVERLAP_S", "plan_chunks", "write_chunk"]
