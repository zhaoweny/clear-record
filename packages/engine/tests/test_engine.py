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


def test_plan_chunks_rejects_non_progressing_overlap() -> None:
    """A reachable `overlap >= chunk` must error, not emit the same window
    forever; ordinary planning is unchanged."""
    from cr_engine import plan_chunks

    with pytest.raises(ValueError):
        plan_chunks(60.0, chunk_s=10.0, overlap_s=10.0)
    with pytest.raises(ValueError):
        plan_chunks(60.0, chunk_s=10.0, overlap_s=25.0)

    chunks = plan_chunks(25.0, chunk_s=10.0, overlap_s=2.0)
    assert chunks[0] == (0.0, 10.0)
    assert chunks[-1][1] == 25.0
    # starts strictly increase: planning always advances
    assert all(
        nxt_start > start for (start, _), (nxt_start, _) in zip(chunks, chunks[1:])
    )


def test_clean_segments_filters_non_speech_and_collapses_loops() -> None:
    from cr_engine import clean_segments

    def seg(start: float, text: str) -> Segment:
        return Segment(start=start, end=start + 1.0, text=text, source="src")

    raw = [
        seg(0.0, "[S]"),
        seg(1.0, "(music)"),
        seg(2.0, "♪♪"),
        seg(3.0, "   "),
        seg(4.0, "hello"),
        seg(5.0, "hello"),  # a decoder repetition loop
        seg(6.0, "world"),
    ]
    out = clean_segments(raw)
    # markers/blank dropped, the loop collapsed, genuine utterances kept
    assert [s.text for s in out] == ["hello", "world"]


def test_estimate_offset_recovers_window_beyond_previous_limit(tmp_path) -> None:
    """The most energetic reference window can sit far into a long tape; the
    offset must still be recovered (the old one-sided guard zeroed it)."""
    sr = 8000
    ref = tmp_path / "ref.wav"  # event at ref time ~200 s
    src = tmp_path / "src.wav"  # same event at source time ~197 s
    _write(ref, leading_silence_s=200.0, sr=sr)
    _write(src, leading_silence_s=197.0, sr=sr)

    offset, confidence = estimate_offset(str(ref), str(src))
    assert offset == pytest.approx(3.0, abs=0.5)
    assert confidence is not None and confidence > 0.0


def test_estimate_offset_unplaceable_source_is_unresolved(tmp_path) -> None:
    from cr_core import Source
    from cr_engine import align_sources

    ref = tmp_path / "ref.wav"
    _write(ref, leading_silence_s=2.0)

    # an unreadable source: confidence must be None, not a fake 0.0
    missing = tmp_path / "missing.wav"
    _, conf_missing = estimate_offset(str(ref), str(missing))
    assert conf_missing is None

    # a source too short to place is equally unresolved
    short = tmp_path / "short.wav"
    sf.write(str(short), np.zeros(100, dtype=np.float32), 8000)
    _, conf_short = estimate_offset(str(ref), str(short))
    assert conf_short is None

    sources = [
        Source(id="ref", path=str(ref), label="Alice"),
        Source(id="bad", path=str(missing), label="Bob"),
    ]
    alignment = align_sources(sources, reference_id="ref")
    assert alignment.unresolved == ("bad",)
    assert "bad" not in alignment.offsets


def test_estimate_offset_uncorrelated_noise_is_unresolved(tmp_path) -> None:
    """Unrelated audio yields only a weak positive correlation peak; below the
    confidence floor it must be unplaceable, not recorded as a fake offset."""
    from cr_core import Source
    from cr_engine import align_sources

    ref = tmp_path / "ref.wav"
    _write(ref, leading_silence_s=2.0)

    # 4 s of unrelated white noise: same length as the reference event, so the
    # only peak is the small correlation of two unrelated signals.
    noise = tmp_path / "noise.wav"
    rng = np.random.default_rng(1856)
    sf.write(str(noise), rng.standard_normal(4 * 8000).astype(np.float32), 8000)

    _, confidence = estimate_offset(str(ref), str(noise))
    assert confidence is None

    sources = [
        Source(id="ref", path=str(ref), label="Alice"),
        Source(id="noise", path=str(noise), label="Bob"),
    ]
    alignment = align_sources(sources, reference_id="ref")
    assert alignment.unresolved == ("noise",)
    assert "noise" not in alignment.offsets


def test_reconcile_collapses_overlap_cluster() -> None:
    """Three mutually-overlapping sources yield exactly one survivor, chosen by
    confidence then source order — not ~ceil(N/2) pairwise survivors."""
    sources = [Source(id=f"s{i}", path="", label=f"spk{i}") for i in range(3)]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0, "s1": 0.0, "s2": 0.0})
    per_source = {
        "s0": [Segment(0.0, 5.0, "hello", "s0", confidence=0.5)],
        "s1": [Segment(1.0, 6.0, "hello", "s1", confidence=0.9)],
        "s2": [Segment(2.0, 7.0, "hello", "s2", confidence=0.7)],
    }

    merged = reconcile(per_source, alignment, sources)
    assert len(merged) == 1
    assert merged[0].speaker == "spk1"  # highest confidence wins


def test_reconcile_survivors_never_overlap() -> None:
    sources = [Source(id=f"s{i}", path="", label=f"spk{i}") for i in range(3)]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0, "s1": 0.0, "s2": 0.0})
    per_source = {
        # one mutually-overlapping cluster near t=0 ...
        "s0": [
            Segment(0.0, 5.0, "alpha", "s0", confidence=0.5),
            # ... and a second, disjoint event later
            Segment(20.0, 25.0, "delta", "s0", confidence=0.4),
        ],
        "s1": [Segment(1.0, 6.0, "beta", "s1", confidence=0.9)],
        "s2": [Segment(2.0, 7.0, "gamma", "s2", confidence=0.7)],
    }

    merged = reconcile(per_source, alignment, sources)
    assert len(merged) == 2
    assert any(s.text == "delta" for s in merged)
    for earlier, later in zip(merged, merged[1:]):
        assert earlier.end <= later.start + 1e-9


def test_reconcile_greedy_keeps_one_of_mutually_overlapping() -> None:
    """A mutually-overlapping cluster still collapses to exactly one survivor,
    chosen by confidence then source order (N -> 1)."""
    sources = [Source(id=f"s{i}", path="", label=f"spk{i}") for i in range(3)]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0, "s1": 0.0, "s2": 0.0})
    per_source = {
        "s0": [Segment(0.0, 5.0, "alpha", "s0", confidence=0.5)],
        "s1": [Segment(1.0, 6.0, "beta", "s1", confidence=0.9)],
        "s2": [Segment(2.0, 7.0, "gamma", "s2", confidence=0.7)],
    }

    merged = reconcile(per_source, alignment, sources)
    assert len(merged) == 1
    assert merged[0].speaker == "spk1"  # highest confidence wins


def test_reconcile_keeps_bridged_but_distinct_segments() -> None:
    """Overlap resolution is not transitive.

    ``s1`` [4, 6.5] bridges the two ``s0`` utterances: it overlaps the
    high-confidence [6, 10] (so it loses) but is the only segment overlapping
    [0, 5]. The old connected-component resolver absorbed [0, 5] into the
    cluster through the bridge and then dropped it, silently losing an
    utterance; the greedy resolver keeps both distinct events."""
    sources = [
        Source(id="s0", path="", label="spk0"),
        Source(id="s1", path="", label="spk1"),
    ]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0, "s1": 0.0})
    per_source = {
        "s0": [
            Segment(0.0, 5.0, "first", "s0", confidence=0.2),
            Segment(6.0, 10.0, "second", "s0", confidence=0.9),
        ],
        "s1": [Segment(4.0, 6.5, "bridge", "s1", confidence=0.5)],
    }

    merged = reconcile(per_source, alignment, sources)
    assert sorted(s.text for s in merged) == ["first", "second"]
    for earlier, later in zip(merged, merged[1:]):
        assert earlier.end <= later.start + 1e-9


def test_reconcile_caps_cue_length() -> None:
    """A long continuous same-speaker run is split so no cue exceeds the cap."""
    sources = [Source(id="s0", path="", label="Alice")]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0})
    # 50 s of contiguous 5 s utterances (distinct text: not a repetition loop)
    per_source = {
        "s0": [
            Segment(i * 5.0, (i + 1) * 5.0, f"word {i}", "s0", confidence=0.5)
            for i in range(10)
        ]
    }

    merged = reconcile(per_source, alignment, sources, max_cue_s=30.0)
    assert len(merged) > 1  # the run was actually split
    assert all(s.end - s.start <= 30.0 + 1e-9 for s in merged)


def test_diarize_auto_keeps_single_speaker() -> None:
    """Auto model-count must stay conservative on a one-speaker scene instead
    of inventing a split."""
    from cr_engine import SYNTH_SR, diarize, make_scene

    scene, events = make_scene(duration_s=25.0, n_speakers=1, seed=0)
    segments = [(e["start"], e["end"]) for e in events]

    labels = diarize(scene, SYNTH_SR, segments)
    assert len(set(labels)) == 1


def test_diarize_auto_splits_two_separated_voices() -> None:
    """Auto model-count can actually *find* a split: two strongly separated
    voices (low-pass vs high-pass noise, silhouette ~0.51) must yield exactly
    two speakers when ``n_speakers`` is left to auto."""
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

    labels = diarize(audio, sr, segments)  # n_speakers=None -> auto
    assert len(set(labels)) == 2


def test_diarize_auto_accepts_marginal_two_voice() -> None:
    """A moderately separated two-voice scene (silhouette ~0.43) is still split.

    This pins ``_MIN_SILHOUETTE``: the old 0.45 floor left this scene at one
    speaker, so the assertion fails if the floor is raised back."""
    from cr_engine import diarize

    sr = 16000
    seg_len = sr
    # A shared component softens the spectral separation just enough that the
    # auto silhouette lands between the old (0.45) and new (0.40) floors.
    common = np.random.default_rng(0).standard_normal(seg_len)
    rng = np.random.default_rng(0)

    def low() -> np.ndarray:
        x = rng.standard_normal(seg_len)
        return np.convolve(x, np.ones(64) / 64, mode="same")

    def high() -> np.ndarray:
        x = rng.standard_normal(seg_len)
        slow = np.convolve(x, np.ones(64) / 64, mode="same")
        return x - slow

    seq = []
    for i in range(4):
        voice = low() if i % 2 == 0 else high()
        seq.append(voice + common)
    audio = np.concatenate(seq).astype(np.float32)
    audio /= np.max(np.abs(audio)) or 1.0
    segments = [(i * 1.0, (i + 1) * 1.0) for i in range(4)]

    labels = diarize(audio, sr, segments)  # n_speakers=None -> auto
    assert len(set(labels)) == 2


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
