"""Tests for the opt-in voiced (pitch/F0) synthesis mode in ``clear_record.engine.synth``.

The generator's default stem is aperiodic noise (for align/energy tests). Voiced
mode adds a harmonic glottal source for pitch experiments. These tests pin both
ends: the noise default stays aperiodic and byte-identical when the voiced-only
parameters are passed with ``voiced=False``, and voiced stems carry a real F0 a
pitch-only split can use to separate two speakers by a set semitone gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from clear_record.engine import DEFAULT_F0_HZ, SYNTH_SR, make_scene, make_speaker_stems
from clear_record.engine.diarize import pitch_stats


def _periodicity_at(x: np.ndarray, sr: int, f0_hz: float) -> float:
    """Normalized autocorrelation of ``x`` at the lag for ``f0_hz``.

    Near 1 for a signal periodic at that F0; near 0 for aperiodic noise.
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return 0.0
    x = (x - x.mean()) * np.hanning(x.size)
    ac = np.correlate(x, x, mode="full")[x.size - 1 :]
    ac = ac / (ac[0] + 1e-12)
    return float(ac[int(round(sr / f0_hz))])


def _pitch_split(
    scene: np.ndarray, events: list[dict], sr: int, threshold_hz: float
) -> list[int]:
    """Pitch-only split: assign each event window to a voice by its F0.

    Uses no energy, no spectral fingerprint and no source labels -- only the
    estimated F0 of the window versus a single threshold between the two voices.
    """

    def guess(e: dict) -> int:
        window = scene[int(e["start"] * sr) : int(e["end"] * sr)]
        return int(pitch_stats(window, sr)[0] >= threshold_hz)

    return [guess(e) for e in events]


def test_voiced_stem_is_periodic_at_f0() -> None:
    """A voiced stem really is voiced: a strong autocorrelation peak at the
    requested F0 and a pitch estimator that recovers it."""
    f0_hz = 120.0
    stems, events = make_speaker_stems(
        8.0, 1, seed=0, voiced=True, f0_hz=f0_hz, non_overlapping=True
    )
    assert events, "scene must contain utterances"
    stem = stems[0]
    assert _periodicity_at(stem, SYNTH_SR, f0_hz) > 0.6
    measured, _iqr, voiced_fraction = pitch_stats(stem, SYNTH_SR)
    assert measured == pytest.approx(f0_hz, rel=0.05)
    assert voiced_fraction > 0.5


def test_noise_default_is_aperiodic_and_opt_in_is_additive() -> None:
    """The default path is unchanged: still aperiodic, and passing the voiced-only
    parameters with ``voiced=False`` yields byte-identical stems and timeline."""
    plain_stems, plain_events = make_speaker_stems(8.0, 2, seed=3)
    opt_out_stems, opt_out_events = make_speaker_stems(
        8.0, 2, seed=3, voiced=False, f0_hz=200.0, f0_gap_semitones=7.0
    )
    assert plain_events == opt_out_events
    for plain, opt_out in zip(plain_stems, opt_out_stems):
        assert np.array_equal(plain, opt_out)
    # No harmonic F0 to be found: the default is aperiodic by construction.
    mixed = np.sum(plain_stems, axis=0)
    assert abs(_periodicity_at(mixed, SYNTH_SR, DEFAULT_F0_HZ)) < 0.3


@pytest.mark.parametrize("gap", [12.0, 7.0, 3.0, 1.0, 0.0])
def test_pitch_split_separates_by_f0_gap(gap: float) -> None:
    """A pitch-only split separates two voiced speakers when their F0s differ and
    cannot when the gap is 0 semitones (both speakers share one F0)."""
    base = 110.0
    stems, events = make_speaker_stems(
        16.0,
        2,
        seed=7,
        voiced=True,
        f0_hz=base,
        f0_gap_semitones=gap,
        non_overlapping=True,
    )
    measured = [pitch_stats(stem, SYNTH_SR)[0] for stem in stems]
    separation = abs(measured[1] - measured[0])
    truth = [e["speaker"] for e in events]

    if gap > 0:
        expected = base * (2.0 ** (gap / 12.0) - 1.0)
        assert separation > 0.5 * expected, "voiced stems must show distinct F0s"
        threshold = base * 0.5 * (1.0 + 2.0 ** (gap / 12.0))
        guesses = _pitch_split(np.sum(stems, axis=0), events, SYNTH_SR, threshold)
        accuracy = sum(g == t for g, t in zip(guesses, truth)) / len(truth)
        assert accuracy >= 0.9, "pitch should separate distinct F0s"
    else:
        assert separation < 2.0, "a 0-semitone gap has no F0 difference"
        threshold = base
        guesses = _pitch_split(np.sum(stems, axis=0), events, SYNTH_SR, threshold)
        accuracy = sum(g == t for g, t in zip(guesses, truth)) / len(truth)
        # Identical F0 cannot decide the speaker. A pitch threshold at the shared
        # F0 is chance: across 15 seeds this measured at most 0.64, so 0.75 is a
        # firm bound that still leaves headroom for seed variation.
        assert accuracy < 0.75, "identical F0 cannot decide the speaker"


def test_voiced_scene_sums_to_stems() -> None:
    """Voiced mode keeps the stem/scene contract: stems sum to ``make_scene`` and
    share its event timeline."""
    stems, events = make_speaker_stems(
        12.0, 3, seed=2, voiced=True, f0_hz=150.0, f0_gap_semitones=3.0
    )
    scene, scene_events = make_scene(
        12.0, 3, seed=2, voiced=True, f0_hz=150.0, f0_gap_semitones=3.0
    )
    assert events == scene_events
    assert np.allclose(np.sum(stems, axis=0), scene, atol=1e-6)


def test_explicit_per_speaker_f0_sequence() -> None:
    """An explicit per-speaker ``f0_hz`` sequence is honoured."""
    f0s = [110.0, 220.0]
    stems, _events = make_speaker_stems(
        8.0, 2, seed=1, voiced=True, f0_hz=f0s, non_overlapping=True
    )
    measured = [pitch_stats(stem, SYNTH_SR)[0] for stem in stems]
    assert measured[0] == pytest.approx(f0s[0], rel=0.05)
    assert measured[1] == pytest.approx(f0s[1], rel=0.05)


def test_f0_sequence_length_must_match_speakers() -> None:
    with pytest.raises(ValueError, match="f0_hz"):
        make_speaker_stems(4.0, 3, voiced=True, f0_hz=[110.0, 220.0])


@pytest.mark.parametrize("scalar", [120.0, np.float64(120.0), np.array(120.0)])
def test_scalar_f0_accepts_python_numpy_and_zero_dim(scalar) -> None:
    """A scalar base F0 may be a Python float, a numpy scalar or a 0-d array.

    Regression: ``np.isscalar`` reports a 0-d array as a sequence, so it was
    iterated and raised ``TypeError``.
    """
    stems, _events = make_speaker_stems(
        8.0, 1, seed=0, voiced=True, f0_hz=scalar, non_overlapping=True
    )
    measured, _iqr, _voiced = pitch_stats(stems[0], SYNTH_SR)
    assert measured == pytest.approx(120.0, rel=0.05)


@pytest.mark.parametrize("bad_f0", [0.0, -10.0])
def test_non_positive_f0_raises(bad_f0: float) -> None:
    """A non-positive F0 would be a degenerate silent source; reject it loudly."""
    with pytest.raises(ValueError, match="f0_hz must be > 0"):
        make_speaker_stems(4.0, 1, voiced=True, f0_hz=bad_f0)


def test_non_positive_per_speaker_f0_raises() -> None:
    with pytest.raises(ValueError, match="f0_hz values must be > 0"):
        make_speaker_stems(4.0, 2, voiced=True, f0_hz=[110.0, 0.0])


def test_negative_gap_raises() -> None:
    with pytest.raises(ValueError, match="f0_gap_semitones must be >= 0"):
        make_speaker_stems(4.0, 1, voiced=True, f0_hz=120.0, f0_gap_semitones=-1.0)
