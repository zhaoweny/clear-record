"""Tests for cr-engine alignment and reconciliation."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from cr_core import Alignment, Segment, Source
from cr_engine.align import cross_correlate, estimate_offset
from cr_engine.merge import reconcile


def _event(duration_s: float = 4.0, sr: int = 8000) -> np.ndarray:
    """A chirp + noise burst — aperiodic, so cross-correlation is unambiguous."""
    rng = np.random.default_rng(7)
    t = np.arange(int(duration_s * sr), dtype=np.float64) / sr
    f0, f1 = 120.0, 1200.0
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2.0 * duration_s))
    chirp = np.sin(phase)
    envelope = np.hanning(t.size).astype(np.float64)
    return ((chirp + 0.1 * rng.standard_normal(t.size)) * envelope).astype(np.float32)


def _write(path, *, leading_silence_s: float, sr: int = 8000) -> None:
    sig = _event(sr=sr)
    pad = np.zeros(int(leading_silence_s * sr), dtype=np.float32)
    sf.write(str(path), np.concatenate([pad, sig]), sr)


def test_cross_correlate_recovers_known_shift(tmp_path) -> None:
    sr = 8000
    rng = np.random.default_rng(0)
    sig = rng.standard_normal(int(4.0 * sr)).astype(np.float32)
    pos = int(1.7 * sr)
    window = sig[pos : pos + int(0.5 * sr)]

    corr = cross_correlate(sig, window)
    # The output index IS the source position of the window's first sample.
    assert int(np.argmax(corr)) == pos


def test_estimate_offset_known_delay(tmp_path) -> None:
    sr = 8000
    ref = tmp_path / "ref.wav"  # event at ref_time ~5s
    src = tmp_path / "src.wav"  # event at source_time ~2s
    _write(ref, leading_silence_s=5.0, sr=sr)
    _write(src, leading_silence_s=2.0, sr=sr)

    offset, confidence = estimate_offset(str(ref), str(src))
    assert offset == pytest.approx(3.0, abs=0.5)
    assert confidence > 0.0


def test_reconcile_shifts_and_prefers_best(tmp_path) -> None:
    alignment = Alignment(reference="ref", offsets={"ref": 0.0, "src": 0.5})
    sources = [
        Source(id="ref", path=str(tmp_path), label="Alice"),
        Source(id="src", path=str(tmp_path), label="Bob"),
    ]

    # src segment local [0.5, 1.5] -> shifted to [1.0, 2.0] on ref timeline,
    # which overlaps the ref segment [0.0, 2.0].
    per_source = {
        "ref": [
            Segment(
                start=0.0, end=2.0, text="hello there", source="ref", confidence=0.9
            ),
        ],
        "src": [
            Segment(
                start=0.5, end=1.5, text="hello there", source="src", confidence=0.6
            ),
        ],
    }
    merged = reconcile(per_source, alignment, sources)
    # The overlapping "hello there" from src (0.6) should lose to ref (0.9).
    assert len(merged) == 1
    assert merged[0].speaker == "Alice"
    assert merged[0].start == 0.0
