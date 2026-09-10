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


def test_synth_align_recovers_true_offsets(tmp_path) -> None:
    """Owner strategy: synthesize 4-device badness with known ground truth, then
    confirm `align` recovers the offsets within a reasonable tolerance."""
    import soundfile as sf

    from cr_core import Source
    from cr_engine import SYNTH_SR, align_sources, make_scene, record

    scene, _ = make_scene(duration_s=25.0, n_speakers=4, seed=1)
    offsets_s = {0: 0.0}
    sources: list[Source] = []
    for i in range(4):
        start_s = 0.0 if i == 0 else [0.40, 0.85, 1.30][i - 1]
        audio, _ = record(
            scene,
            start_s=start_s,
            drift_ppm=-25.0,
            gain=1.05,
            noise=0.002,
            lowpass_ms=0.6,
            rir_s=0.08,
            dropout_frac=0.005,
            seed=7,
        )
        wav = tmp_path / f"d{i}.wav"
        sf.write(str(wav), audio, SYNTH_SR)
        offsets_s[i] = start_s
        sources.append(Source(id=f"d{i}", path=str(wav), label=f"dev{i}"))

    alignment = align_sources(sources, reference_id="d0")
    for i in range(1, 4):
        got = alignment.offsets[f"d{i}"]
        assert got == pytest.approx(offsets_s[i], abs=0.15), f"device {i} offset {got}"


def test_channel_count_and_channel_select(tmp_path) -> None:
    import soundfile as sf

    from cr_engine import channel_count, read_audio

    sr = 8000
    t = np.arange(sr, dtype=np.float64) / sr
    left = np.sin(2 * np.pi * 200.0 * t).astype(np.float32)
    right = np.sin(2 * np.pi * 700.0 * t).astype(np.float32)
    p = tmp_path / "stereo.wav"
    sf.write(str(p), np.stack([left, right], axis=1), sr)

    assert channel_count(p) == 2
    got_l, _ = read_audio(p, channel=0)
    got_r, _ = read_audio(p, channel=1)
    # channels must not be collapsed together (the meeting-tape isolation case)
    assert not np.allclose(got_l, got_r)
    # default downmixes to mono
    mixed, _ = read_audio(p)
    assert mixed.shape == got_l.shape


def test_plan_chunks_overlap() -> None:
    from cr_engine import plan_chunks

    assert plan_chunks(5.0, chunk_s=10.0, overlap_s=2.0) == [(0.0, 5.0)]
    chunks = plan_chunks(25.0, chunk_s=10.0, overlap_s=2.0)
    assert chunks[0] == (0.0, 10.0)
    assert chunks[-1][1] == 25.0
    # every boundary < duration is followed by an overlapping start
    for (_, end), (nxt_start, _) in zip(chunks, chunks[1:]):
        assert nxt_start == end - 2.0


def test_diarize_separates_two_voices(tmp_path) -> None:
    from cr_engine import diarize

    sr = 16000
    rng = np.random.default_rng(0)
    seg_len = sr

    def low() -> np.ndarray:
        x = rng.standard_normal(seg_len)
        return np.convolve(x, np.ones(64) / 64, mode="same")

    def high() -> np.ndarray:
        x = rng.standard_normal(seg_len)
        slow = np.convolve(x, np.ones(64) / 64, mode="same")
        return x - slow

    seq = [low(), high(), low(), high()]
    audio = np.concatenate(seq).astype(np.float32)
    audio /= np.max(np.abs(audio)) or 1.0
    segments = [(i * 1.0, (i + 1) * 1.0) for i in range(4)]

    labels = diarize(audio, sr, segments, n_speakers=2)
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    assert labels[0] != labels[1]


def test_diarize_separates_two_pitches() -> None:
    """Pitch is the strongest cheap cue: two harmonic voices an octave apart."""
    from cr_engine import diarize

    sr = 16000
    n = sr

    def harmonic(f0: float) -> np.ndarray:
        t = np.arange(n, dtype=np.float64) / sr
        x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))
        return x.astype(np.float32)

    seq = [harmonic(110.0), harmonic(220.0), harmonic(110.0), harmonic(220.0)]
    audio = np.concatenate(seq)
    audio /= np.max(np.abs(audio)) or 1.0
    segments = [(i * 1.0, (i + 1) * 1.0) for i in range(4)]

    labels = diarize(audio, sr, segments, n_speakers=2)
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    assert labels[0] != labels[1]
