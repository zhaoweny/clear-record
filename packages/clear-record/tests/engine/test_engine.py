"""Tests for clear_record.engine alignment and reconciliation."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from clear_record.core import Alignment, Segment, Source
from clear_record.engine.align import cross_correlate, estimate_offset
from clear_record.engine.merge import reconcile


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


def _speech_like(n: int, *, seed: int) -> np.ndarray:
    """Aperiodic speech-shaped noise (band-limited, syllabic): a good anchor."""
    rng = np.random.default_rng(seed)
    base = np.convolve(rng.standard_normal(n), np.ones(4) / 4, mode="same")
    env = 0.4 + 0.6 * np.abs(np.sin(np.linspace(0.0, 10.0 * np.pi, n)))
    env *= 0.5 + 0.5 * rng.random(n)
    x = base * env
    return (x / max(float(np.max(np.abs(x))), 1e-9) * 0.8).astype(np.float32)


def _dissimilar_copy(x: np.ndarray, *, seed: int, noise: float) -> np.ndarray:
    """A second capture chain: small/band-limited capsule + independent noise."""
    band = np.convolve(x, np.ones(3) / 3, mode="same").astype(np.float32)
    rng = np.random.default_rng(seed)
    return (band * 0.3 + rng.standard_normal(x.size) * noise).astype(np.float32)


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

    from clear_record.core import Source
    from clear_record.engine import SYNTH_SR, align_sources, make_scene, record

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

    from clear_record.engine import channel_count, read_audio

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
    from clear_record.engine import plan_chunks

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
    from clear_record.engine import plan_chunks

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
    from clear_record.engine import clean_segments

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


def test_estimate_offset_phone_memo_across_dissimilar_mic(tmp_path) -> None:
    """A phone memo (attenuated, band-limited, different mic) is placed 300 s
    from the reference window, even when the reference's loudest stretch is not
    on the phone at all.

    The reference opens with a loud transient the phone missed, so the single
    most energetic 4 s window lands there and correlates to nothing: the old
    single-window, absolute-0.25 code returned ``unresolved`` for this tape. The
    spread windows find the shared content, and their isolated peaks stand well
    above the correlation background. ``max_lag_s`` is widened to match the
    observed ~+357 s separation on a real multi-mic tape.
    """
    sr = 2000  # written rate; read_audio resamples to _ALIGN_SR (1 kHz)
    shared = _speech_like(int(180.0 * sr), seed=5)
    burst = _speech_like(int(5.0 * sr), seed=77) * 1.5  # phone missed this
    quiet = _speech_like(int(295.0 * sr), seed=78) * 0.05  # quiet room tone
    ref = np.concatenate([burst, quiet, shared]).astype(np.float32)
    phone = _dissimilar_copy(shared, seed=3, noise=0.2)  # started 300 s earlier
    ref_p = tmp_path / "ref.wav"
    phone_p = tmp_path / "phone.wav"
    sf.write(str(ref_p), ref, sr)
    sf.write(str(phone_p), phone, sr)

    offset, confidence = estimate_offset(str(ref_p), str(phone_p), max_lag_s=400.0)
    # shared content sits 300 s later in the reference: ref_time = source_time + 300
    assert offset == pytest.approx(300.0, abs=0.5)
    assert confidence is not None and confidence > 0.0


def test_estimate_offset_processed_builtin_mic_resolves(tmp_path) -> None:
    """A processed, band-limited copy with a modest offset resolves.

    Its normalized coefficient (~0.17) sits *below* the old 0.25 floor, so the
    single-window absolute threshold rejected it; the peak is nonetheless far
    more prominent than the correlation background, so the prominence criterion
    accepts it. The winning coefficient stays below 0.25 to pin that difference.
    """
    sr = 2000
    x = _speech_like(int(60.0 * sr), seed=5)
    shift = int(8 * sr)
    builtin = _dissimilar_copy(x, seed=4, noise=0.3)
    src = np.concatenate([np.zeros(shift, np.float32), builtin])[: x.size]
    ref_p = tmp_path / "ref.wav"
    src_p = tmp_path / "builtin.wav"
    sf.write(str(ref_p), x, sr)
    sf.write(str(src_p), src, sr)

    offset, confidence = estimate_offset(str(ref_p), str(src_p))
    # the source is delayed by 8 s, so ref_time = source_time - 8
    assert offset == pytest.approx(-8.0, abs=0.5)
    assert confidence is not None
    assert 0.0 < confidence < 0.25


def test_estimate_offset_attenuated_shared_region_corroborates(tmp_path) -> None:
    """A genuine shared passage ~12 dB below a loud anchor still places the source.

    The reference opens with a loud stretch the source never recorded, so a 5%
    relative-energy filter drops the quieter — but genuine — shared windows,
    collapsing the candidate list to the one bad anchor and refusing the source
    (the ~10 dB recall cliff). The absolute near-silence floor keeps every
    non-silent window, and the five shared windows agree on +5 s.
    """
    sr = 2000
    loud = _speech_like(int(5.0 * sr), seed=77) * 1.4  # source missed this
    shared = _speech_like(int(60.0 * sr), seed=5) * 0.35  # ~12 dB down
    ref = np.concatenate([loud, shared]).astype(np.float32)
    src = _dissimilar_copy(shared, seed=3, noise=0.1)
    ref_p = tmp_path / "ref.wav"
    src_p = tmp_path / "shared.wav"
    sf.write(str(ref_p), ref, sr)
    sf.write(str(src_p), src, sr)

    offset, confidence = estimate_offset(str(ref_p), str(src_p))
    assert offset == pytest.approx(5.0, abs=0.5)
    assert confidence is not None and confidence > 0.0


def test_estimate_offset_same_device_pair_resolves(tmp_path) -> None:
    """The common case still resolves with a high, scale-invariant confidence."""
    sr = 2000
    x = _speech_like(int(60.0 * sr), seed=9)
    shift = int(5 * sr)
    src = np.concatenate([np.zeros(shift, np.float32), x])[: x.size]
    ref_p = tmp_path / "ref.wav"
    src_p = tmp_path / "same.wav"
    sf.write(str(ref_p), x, sr)
    sf.write(str(src_p), src, sr)

    offset, confidence = estimate_offset(str(ref_p), str(src_p))
    assert offset == pytest.approx(-5.0, abs=0.5)
    assert confidence is not None and confidence > 0.5


def test_estimate_offset_unplaceable_source_is_unresolved(tmp_path) -> None:
    from clear_record.core import Source
    from clear_record.engine import align_sources

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
    """Unrelated audio yields only a weak, non-prominent correlation peak: it
    must stay unplaceable, not recorded as a fake offset."""
    from clear_record.core import Source
    from clear_record.engine import align_sources

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


def test_estimate_offset_long_reference_lone_weak_peak_is_unresolved(tmp_path) -> None:
    """A long reference must not launder one weak peak through the lone-window path.

    The reference is 5 s of content followed by 115 s of near-silence
    (amplitude 1e-4). A relative-energy filter collapses this to the single loud
    window and accepts its coincidental correlation with unrelated audio (offset
    -27.3 s, confidence ~0.107 on the pre-fix code). Because the near-silence is
    skipped by an absolute floor and the sole surviving vote is weak, the source
    stays unresolved: a lone vote is only trusted when it is a *strong* peak, and
    "the filter left one window" does not count as a one-window reference.
    """
    sr = 2000
    ref = np.concatenate(
        [
            _speech_like(int(5.0 * sr), seed=5) * 0.8,
            _speech_like(int(115.0 * sr), seed=6) * 1e-4,
        ]
    ).astype(np.float32)
    src = np.concatenate(
        [
            np.zeros(int(30 * sr), np.float32),
            _speech_like(int(4 * sr), seed=230),  # unrelated burst-in-silence
            np.zeros(int(30 * sr), np.float32),
        ]
    ).astype(np.float32)
    ref_p = tmp_path / "ref.wav"
    src_p = tmp_path / "unrelated.wav"
    sf.write(str(ref_p), ref, sr)
    sf.write(str(src_p), src, sr)

    _, confidence = estimate_offset(str(ref_p), str(src_p))
    assert confidence is None


def test_estimate_offset_short_speech_band_noise_is_unresolved(tmp_path) -> None:
    """Short uncorrelated speech-band noise must not exploit edge lags.

    On a 6 s reference a large share of the search band used to be
    partial-overlap lags, where a handful of samples normalize into a spuriously
    high cosine; noise then reached prominence ~25 and this seed was accepted at
    confidence 0.111. Bounding the search to full-window overlaps and requiring
    corroboration keeps it unresolved.
    """
    sr = 2000
    a = _speech_like(int(6.0 * sr), seed=10366)
    b = _speech_like(int(6.0 * sr), seed=10367)
    a_p = tmp_path / "a.wav"
    b_p = tmp_path / "b.wav"
    sf.write(str(a_p), a, sr)
    sf.write(str(b_p), b, sr)

    _, confidence = estimate_offset(str(a_p), str(b_p))
    assert confidence is None


def test_estimate_offset_long_recording_confidence_is_scale_invariant(
    tmp_path,
) -> None:
    """A genuine match on a long tape still earns a *high* confidence.

    The old confidence was the raw cross-correlation peak, which falls as
    ~sqrt(window_len / signal_len): at 1200 s it is ~0.06, so the ``> 0.5``
    assertion below fails and the 0.05 floor wrongly rejects every genuine
    alignment past ~2.5 h. The scale-invariant coefficient stays ~1.0 at any
    length.
    """
    from clear_record.engine import align_sources

    sr = 2000  # written rate; read_audio resamples to _ALIGN_SR (1 kHz)
    duration_s = 1200.0
    shift_s = 60.0
    n = int(duration_s * sr)
    rng = np.random.default_rng(11)
    k = max(1, int(0.004 * sr))
    base = np.convolve(rng.standard_normal(n), np.ones(k) / k, mode="same")
    # Confine the signal to the middle so the shifted source keeps every sample:
    # the reference window always has a full-length counterpart.
    envelope = np.zeros(n, dtype=np.float32)
    envelope[int(100.0 * sr) : int(1050.0 * sr)] = 1.0
    sig = (base * envelope).astype(np.float32)
    sig *= 0.5 / (np.max(np.abs(sig)) or 1.0)

    shift = int(shift_s * sr)
    src = np.concatenate([np.zeros(shift, dtype=np.float32), sig[: n - shift]])
    ref_p = tmp_path / "ref_long.wav"
    src_p = tmp_path / "src_long.wav"
    sf.write(str(ref_p), sig, sr)
    sf.write(str(src_p), src, sr)

    offset, confidence = estimate_offset(str(ref_p), str(src_p))
    # src is delayed by 60 s, so ref_time = source_time - 60.
    assert offset == pytest.approx(-shift_s, abs=0.5)
    assert confidence is not None and confidence > 0.5

    alignment = align_sources(
        [
            Source(id="ref", path=str(ref_p), label="Alice"),
            Source(id="src", path=str(src_p), label="Bob"),
        ],
        reference_id="ref",
    )
    assert alignment.unresolved == ()
    assert alignment.confidence is not None and alignment.confidence > 0.5


def test_estimate_offset_long_uncorrelated_noise_is_unresolved(tmp_path) -> None:
    """Independent noise of realistic length stays below the prominence floor.

    With the search bounded to full-window overlaps, spurious correlation peaks
    over many seeds of speech-band noise measure <=0.13 in coefficient and
    <=9.40 in ``(peak - median) / MAD`` prominence across 6-600 s (300
    seeds/length); the 10.0 floor plus required corroboration keeps the source
    unresolved. (Unbounded, partial-overlap edge lags pushed short noise to
    prominence ~10.4 at 20 s and ~25.7 at 6 s — the reviewer's finding — so the
    bound is load-bearing, not cosmetic.) A lone spurious peak also cannot clear
    the strong-peak bar.
    """
    from clear_record.engine import align_sources

    sr = 2000
    n = int(1200.0 * sr)
    k = max(1, int(0.004 * sr))

    def noise(seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        x = np.convolve(rng.standard_normal(n), np.ones(k) / k, mode="same")
        x *= 0.5 / (np.max(np.abs(x)) or 1.0)
        return x.astype(np.float32)

    ref_p = tmp_path / "ref_noise.wav"
    src_p = tmp_path / "src_noise.wav"
    sf.write(str(ref_p), noise(1), sr)
    sf.write(str(src_p), noise(2), sr)

    _, confidence = estimate_offset(str(ref_p), str(src_p))
    assert confidence is None

    sources = [
        Source(id="ref", path=str(ref_p), label="Alice"),
        Source(id="noise", path=str(src_p), label="Bob"),
    ]
    alignment = align_sources(sources, reference_id="ref")
    assert alignment.unresolved == ("noise",)
    assert "noise" not in alignment.offsets


def test_estimate_offset_resolves_reverberant_source(tmp_path) -> None:
    """A genuine but strongly reverberant source resolves at low coefficient.

    A *dry* reference against a same-content source with ``rir_s`` 0.16 smears
    the coefficient down to ~0.17-0.23 — above the old 0.05 floor, below the
    superseded 0.25 absolute floor (ticket 04's precision-over-recall tradeoff,
    which wrongly hid microphones that genuinely share content). The peak is
    still far more prominent than the correlation background (prominence ~19-27),
    so the prominence criterion places it. If the criterion regresses to an
    absolute 0.25 floor this fails.
    """
    from clear_record.engine import SYNTH_SR, make_scene, record

    scene, _ = make_scene(duration_s=10.0, n_speakers=3, seed=1)
    ref, _ = record(scene, start_s=0.0, rir_s=0.0, seed=1)
    src, _ = record(scene, start_s=1.5, rir_s=0.16, seed=2)
    ref_p = tmp_path / "dry_ref.wav"
    src_p = tmp_path / "reverberant_src.wav"
    sf.write(str(ref_p), ref, SYNTH_SR)
    sf.write(str(src_p), src, SYNTH_SR)

    offset, confidence = estimate_offset(str(ref_p), str(src_p))
    assert confidence is not None
    # Placeable despite the coefficient falling under the old 0.25 floor.
    assert 0.15 < confidence < 0.25
    assert offset == pytest.approx(1.5, abs=0.5)


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
    """A long same-speaker run bridged across real pauses is split at the cap."""
    sources = [Source(id="s0", path="", label="Alice")]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0})
    # 50 s of 4.9 s utterances with a 0.1 s real pause between them (distinct
    # text, so not a repetition loop): only a real pause joins, so the run
    # merges and is then split by the cap.
    per_source = {
        "s0": [
            Segment(i * 5.0, i * 5.0 + 4.9, f"word {i}", "s0", confidence=0.5)
            for i in range(10)
        ]
    }

    merged = reconcile(per_source, alignment, sources, max_cue_s=30.0)
    assert len(merged) > 1  # the run was actually split
    assert all(s.end - s.start <= 30.0 + 1e-9 for s in merged)
    # The join actually happened: the first cue spans several 5 s utterances.
    assert merged[0].end - merged[0].start > 10.0


def test_reconcile_keeps_touching_sentence_cues_separate() -> None:
    """Regression: contiguous ASR cues are distinct sentences.

    The observed bug: ``_join_continuous`` bridged touching cues
    (``end == start``) and collapsed eight sentence cues into one 26 s cue --
    useless as an SRT/VTT subtitle and a blur of the transcript's timeline.
    """
    sources = [Source(id="s0", path="", label="Speaker 1")]
    alignment = Alignment(reference="s0", offsets={"s0": 0.0})
    per_source = {
        "s0": [
            Segment(0.0, 4.8, "第一句", "s0"),
            Segment(4.8, 7.68, "第二句", "s0"),
            Segment(7.68, 12.12, "第三句", "s0"),
        ]
    }

    merged = reconcile(per_source, alignment, sources)
    assert [s.text for s in merged] == ["第一句", "第二句", "第三句"]


def test_reconcile_never_names_a_speaker_after_the_tape() -> None:
    """An unlabelled source gets a generic speaker, never its file-name id."""
    sources = [Source(id="wuhe-16k", path="", label="wuhe-16k")]
    alignment = Alignment(reference="wuhe-16k", offsets={"wuhe-16k": 0.0})
    per_source = {"wuhe-16k": [Segment(0.0, 4.8, "你好", "wuhe-16k")]}

    merged = reconcile(per_source, alignment, sources)
    assert merged[0].speaker == "Speaker 1"


def test_reconcile_keeps_an_explicit_speaker_label() -> None:
    sources = [Source(id="mic-a", path="", label="Alice")]
    alignment = Alignment(reference="mic-a", offsets={"mic-a": 0.0})
    per_source = {"mic-a": [Segment(0.0, 4.8, "hi", "mic-a")]}

    merged = reconcile(per_source, alignment, sources)
    assert merged[0].speaker == "Alice"


def test_clean_segments_tidies_cjk_punctuation() -> None:
    """A stray leading 。 and a space before a CJK comma are not transcript."""
    from clear_record.engine import clean_segments

    out = clean_segments([Segment(0.0, 1.0, "。我认为应当这样", "s0")])
    assert out[0].text == "我认为应当这样"

    spaced = clean_segments([Segment(0.0, 1.0, "就个人而言 ，需要", "s0")])
    assert spaced[0].text == "就个人而言，需要"

    assert clean_segments([Segment(0.0, 1.0, "。", "s0")]) == []


def test_diarize_auto_keeps_single_speaker() -> None:
    """Auto model-count must stay conservative on a one-speaker scene instead
    of inventing a split."""
    from clear_record.engine import SYNTH_SR, diarize, make_scene

    scene, events = make_scene(duration_s=25.0, n_speakers=1, seed=0)
    segments = [(e["start"], e["end"]) for e in events]

    labels = diarize(scene, SYNTH_SR, segments)
    assert len(set(labels)) == 1


def test_diarize_auto_single_speaker_worst_case() -> None:
    """Pin the *worst* single-speaker scene, not an easy one.

    Across durations 6-30 s and seeds 0-19 the highest silhouette any
    single-speaker ``make_scene`` reaches is ~0.399 (8 s, seed 1), the tightest
    case against ``_MIN_SILHOUETTE``. Auto mode must still call it one speaker:
    lowering the floor below ~0.399 makes this scene split and fails here."""
    from clear_record.engine import SYNTH_SR, diarize, make_scene

    scene, events = make_scene(duration_s=8.0, n_speakers=1, seed=1)
    segments = [(e["start"], e["end"]) for e in events]

    labels = diarize(scene, SYNTH_SR, segments)
    assert len(set(labels)) == 1


def test_diarize_auto_splits_two_separated_voices() -> None:
    """Auto model-count can actually *find* a split: two strongly separated
    voices (low-pass vs high-pass noise, silhouette ~0.51) must yield exactly
    two speakers when ``n_speakers`` is left to auto."""
    from clear_record.engine import diarize

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
    from clear_record.engine import diarize

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
    from clear_record.engine import diarize

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
    from clear_record.engine import diarize

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
