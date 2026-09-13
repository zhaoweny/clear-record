"""Tests for cross-talk-aware attribution and the synthetic cross-talk model."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from cr_core import Segment, Source
from cr_engine import (
    SYNTH_SR,
    attribute_segments,
    attribute_segments_windowed,
    make_crosstalk_scene,
    make_scene,
    make_speaker_stems,
    mix_crosstalk,
)


def _write_devices(tmp_path, devices, prefix: str) -> list[Source]:
    sources = []
    for i, audio in enumerate(devices):
        path = tmp_path / f"{prefix}{i}.wav"
        sf.write(str(path), audio, SYNTH_SR)
        sources.append(Source(id=f"{prefix}{i}", path=str(path), label=f"Speaker {i}"))
    return sources


def _accuracy(got: list[str], truth: list[str]) -> float:
    return sum(a == b for a, b in zip(got, truth)) / max(1, len(truth))


def test_make_speaker_stems_sums_to_scene() -> None:
    """The stem split must not change the existing generator: the stems sum to
    exactly the ``make_scene`` mix and share its event timeline."""
    stems, events = make_speaker_stems(duration_s=12.0, n_speakers=3, seed=2)
    scene, scene_events = make_scene(duration_s=12.0, n_speakers=3, seed=2)

    assert len(stems) == 3
    assert events == scene_events
    assert np.allclose(np.sum(stems, axis=0), scene, atol=1e-6)


def test_crosstalk_energy_attribution_beats_naive_source_mapping(tmp_path) -> None:
    """The core ticket-09 regression.

    Two close lavs, each with the *other* speaker bled in 6 dB down. Segments are
    deliberately taken from the bleed-dominated channel (modelling the surviving
    ASR segment after closest-mic-wins), so the naive ``source == speaker`` rule
    is wrong for every segment. Energy attribution must recover the true speaker
    by comparing the aligned window across sources -- and it must be exact, not
    merely better.
    """
    devices, events = make_crosstalk_scene(
        duration_s=20.0, n_speakers=2, bleed_db=-6.0, seed=3, non_overlapping=True
    )
    sources = _write_devices(tmp_path, devices, "lav")

    # One segment per event, but sourced from the *other* lav (the bleed channel).
    segments = [
        Segment(
            start=e["start"],
            end=e["end"],
            text="word",
            source=f"lav{1 - e['speaker']}",
            speaker=f"Speaker {1 - e['speaker']}",
        )
        for e in events
    ]
    truth = [f"Speaker {e['speaker']}" for e in events]
    naive = [s.speaker for s in segments]

    attributed = attribute_segments(segments, sources)

    assert _accuracy(naive, truth) == 0.0  # naive is concretely wrong here
    assert _accuracy([s.speaker for s in attributed], truth) == 1.0
    # attribution re-labels speaker only; source/time/text are untouched
    assert [s.source for s in attributed] == [s.source for s in segments]


def test_clean_per_channel_attribution_is_unchanged(tmp_path) -> None:
    """No regression on isolated channels: with no bleed the incoming per-channel
    attribution is already right, and attribution leaves it byte-for-byte alone."""
    stems, events = make_speaker_stems(
        duration_s=20.0, n_speakers=2, seed=4, non_overlapping=True
    )
    devices = [mix_crosstalk(stems, s, bleed_db=-120.0) for s in range(2)]
    sources = _write_devices(tmp_path, devices, "iso")

    segments = [
        Segment(
            start=e["start"],
            end=e["end"],
            text="word",
            source=f"iso{e['speaker']}",
            speaker=f"Speaker {e['speaker']}",
        )
        for e in events
    ]
    got = attribute_segments(segments, sources)
    assert [s.speaker for s in got] == [s.speaker for s in segments]
    assert all(a.text == b.text and a.start == b.start for a, b in zip(got, segments))


@pytest.mark.parametrize("bleed_db", [-6.0, -9.0, -12.0])
def test_mixed_reference_does_not_hijack_and_gates_unmiked_speech(
    tmp_path, bleed_db: float
) -> None:
    """A room/mixed reference is a witness, not a speaker.

    Three speakers but only two lavs: the third is unmiked, so its utterances are
    present only as quiet bleed. Without a room reference the energy method still
    claims those windows (bleed > silence floor) and misattributes them; with the
    room reference the weak claim is gated and the incoming speaker is preserved.
    On mic-covered events the room reference changes nothing. Parametrized over
    the realistic -6..-12 dB lav bleed range, not only very deep attenuation.
    """
    stems, events = make_speaker_stems(
        duration_s=20.0, n_speakers=3, seed=5, non_overlapping=True
    )
    devices = [mix_crosstalk(stems, s, bleed_db=bleed_db) for s in range(2)]
    candidates = _write_devices(tmp_path, devices, "mic")
    room = np.sum(stems, axis=0).astype(np.float32)
    room_path = tmp_path / "room.wav"
    sf.write(str(room_path), room, SYNTH_SR)
    # The room also appears in ``sources`` (as the CLI passes the manifest); the
    # engine must still exclude it from the candidate set.
    mixed = Source(id="room", path=str(room_path), label="Room")
    sources = [*candidates, mixed]

    segments = [
        Segment(
            start=e["start"],
            end=e["end"],
            text="word",
            source=f"mic{min(e['speaker'], 1)}",
            speaker=f"Speaker {e['speaker']}",
        )
        for e in events
    ]

    without = [s.speaker for s in attribute_segments(segments, candidates)]
    with_mixed = [s.speaker for s in attribute_segments(segments, sources, mixed=mixed)]

    unmiked = [i for i, e in enumerate(events) if e["speaker"] == 2]
    assert unmiked, "scene must contain utterances from the unmiked speaker"
    # the room reference never becomes a speaker, even though its id is a source
    assert "Room" not in with_mixed
    # ... mic-covered segments are unaffected ...
    covered = [i for i, e in enumerate(events) if e["speaker"] < 2]
    for i in covered:
        assert with_mixed[i] == f"Speaker {events[i]['speaker']}"
    # ... and the room gate preserves the incoming speaker for room-only speech
    # that no mic owns, where the ungated run guesses a mic.
    assert any(without[i] != f"Speaker {events[i]['speaker']}" for i in unmiked), (
        "without the room reference the unmiked speech should be misattributed"
    )
    assert all(with_mixed[i] == f"Speaker {events[i]['speaker']}" for i in unmiked)


def test_attribute_segments_uses_source_local_to_reference_offsets(tmp_path) -> None:
    """The offsets path is load-bearing: a segment's window is read on the common
    reference timebase, not at its raw source-local time.

    ``dev1`` starts 3 s after the reference, so a segment it reports at source
    time [0, 2] belongs to reference [3, 5] -- where its own speaker (Bob) is
    active and the reference-source speaker (Alice) is not. Ignoring the offset
    reads the same window on the reference clock, where Alice is active and Bob is
    only half-energetic, and misattributes to Alice."""
    from cr_engine import attribute_segments

    sr = SYNTH_SR
    n = 6 * sr

    def band(seed: int, active: slice) -> np.ndarray:
        rng = np.random.default_rng(seed)
        x = np.convolve(rng.standard_normal(n), np.ones(50) / 50, mode="same")
        mask = np.zeros(n, dtype=np.float32)
        mask[active] = 1.0
        return (x * mask).astype(np.float32)

    alice = band(1, slice(0, 2 * sr))  # fills the read window [0, 2]
    bob = band(2, slice(0, sr))  # only half-fills it
    a_path, b_path = tmp_path / "alice.wav", tmp_path / "bob.wav"
    sf.write(str(a_path), alice, sr)
    sf.write(str(b_path), bob, sr)
    sources = [
        Source(id="dev0", path=str(a_path), label="Alice"),
        Source(id="dev1", path=str(b_path), label="Bob"),
    ]
    segment = Segment(start=0.0, end=2.0, text="hi", source="dev1", speaker="Alice")

    with_offsets = attribute_segments(
        [segment], sources, offsets={"dev0": 0.0, "dev1": 3.0}
    )
    assert with_offsets[0].speaker == "Bob"

    without = attribute_segments([segment], sources, offsets={})
    assert without[0].speaker == "Alice"  # the mapping is what corrects this


def test_attribute_segments_short_or_bad_sources_are_safe(tmp_path) -> None:
    """Missing/short sources are skipped, never fatal; a segment no source can
    carry keeps its incoming speaker."""
    good = np.zeros(SYNTH_SR, dtype=np.float32)
    good[: SYNTH_SR // 2] = 0.1
    good_path = tmp_path / "good.wav"
    sf.write(str(good_path), good, SYNTH_SR)
    sources = [
        Source(id="good", path=str(good_path), label="Alice"),
        Source(id="missing", path=str(tmp_path / "nope.wav"), label="Bob"),
    ]
    # The only readable source is silent in this window, so nobody can claim it.
    segments = [Segment(0.7, 0.9, "hi", "missing", speaker="Bob")]

    got = attribute_segments(segments, sources)
    assert got[0].speaker == "Bob"
    assert got[0].start == pytest.approx(0.7)


# --------------------------------------------------------------------------- #
# Gain-normalized attribution: per-source level, rolling window, confidence
# --------------------------------------------------------------------------- #

_GAIN_BLEED_DB = -9.0
# ±6 dB per device (12 dB total) is the imbalance the synthetic experiment showed
# collapses stateless closest-mic to ~chance once `imb/2 + bleed > 0`.
_GAIN_IMBALANCE_DB = 12.0
# Mid-tape step: device 0 loud first half, device 1 loud second half. Averaging
# the two halves cancels it, so a single static correction cannot track it.
_GAIN_STEP_DB = 8.0
_GAIN_SCENES_S = 120.0


def _write_gained(directory, devices, gains, prefix: str) -> list[Source]:
    """Write devices to wav (optionally × per-sample ``gains``) as Sources."""
    directory.mkdir(parents=True, exist_ok=True)
    sources = []
    for i, audio in enumerate(devices):
        if gains is not None:
            audio = (audio * gains[i]).astype(np.float32)
        path = directory / f"{prefix}{i}.wav"
        sf.write(str(path), audio, SYNTH_SR)
        sources.append(Source(id=f"{prefix}{i}", path=str(path), label=f"Speaker {i}"))
    return sources


def _gain_segments(events: list[dict]) -> list[Segment]:
    """One segment per event, deliberately tagged with the wrong speaker.

    The incoming speaker is not load-bearing (attribution overwrites it), so the
    test scores the recovered speaker against the generator's known truth.
    """
    return [
        Segment(
            start=e["start"],
            end=e["end"],
            text="word",
            source="dev0",
            speaker="Speaker 0",
        )
        for e in events
    ]


def _gain_accuracy(got: list[Segment], events: list[dict]) -> float:
    return sum(
        seg.speaker == f"Speaker {e['speaker']}" for seg, e in zip(got, events)
    ) / max(1, len(events))


def _ece(results: list[Segment], events: list[dict], nbins: int = 10) -> float:
    """Expected calibration error of the emitted confidence (10 equal bins)."""
    conf = np.array([float(s.confidence or 0.0) for s in results])
    correct = np.array(
        [s.speaker == f"Speaker {e['speaker']}" for s, e in zip(results, events)],
        dtype=float,
    )
    edges = np.linspace(0.0, 1.0, nbins + 1)
    ece = 0.0
    for i in range(nbins):
        if i < nbins - 1:
            mask = (conf >= edges[i]) & (conf < edges[i + 1])
        else:
            mask = (conf >= edges[i]) & (conf <= edges[i + 1])
        if not np.any(mask):
            continue
        ece += (mask.sum() / conf.size) * abs(correct[mask].mean() - conf[mask].mean())
    return float(ece)


@pytest.fixture(scope="module")
def gain_scenes(tmp_path_factory):
    """Two synthetic cross-talk scenes under a static and a step gain imbalance.

    Built once per module. Each scene is one lav per speaker with -9 dB cross-talk
    (the repo generator's model), then a per-device gain is applied: constant ±6 dB
    (static) or a mid-tape ±8 dB step that swaps the hot device (time-varying).
    """
    root = tmp_path_factory.mktemp("gain_scenes")
    scenes = []
    for seed in (1, 2):
        devices, events = make_crosstalk_scene(
            _GAIN_SCENES_S,
            2,
            bleed_db=_GAIN_BLEED_DB,
            seed=seed,
            non_overlapping=True,
        )
        half = _GAIN_IMBALANCE_DB / 2.0
        static_gains = [10.0 ** (half / 20.0), 10.0 ** (-half / 20.0)]

        n = devices[0].size
        t = np.arange(n, dtype=np.float64) / SYNTH_SR
        step = np.where(t < t[-1] / 2.0, _GAIN_STEP_DB, -_GAIN_STEP_DB)
        step_gains = [10.0 ** (step / 20.0), 10.0 ** (-step / 20.0)]

        scenes.append(
            {
                "events": events,
                "segments": _gain_segments(events),
                "static_sources": _write_gained(
                    root / f"static{seed}", devices, static_gains, "d"
                ),
                "step_sources": _write_gained(
                    root / f"step{seed}", devices, step_gains, "d"
                ),
            }
        )
    return scenes


def test_gain_normalized_beats_stateless_closest_mic(gain_scenes) -> None:
    """Per-source level normalization decisively beats stateless closest-mic.

    In the cross-talk + static gain-imbalance regime the raw loudest-mic rule is
    near chance; normalizing each source against its own level recovers the true
    speaker with a large margin (the mechanism, not an incidental win).
    """
    stateless_acc, normalized_acc = [], []
    for scene in gain_scenes:
        segs, events = scene["segments"], scene["events"]
        # gain_normalize=False is the control: raw window energy, no correction.
        stateless = attribute_segments_windowed(
            segs, scene["static_sources"], gain_normalize=False
        )
        normalized = attribute_segments_windowed(segs, scene["static_sources"])
        stateless_acc.append(_gain_accuracy(stateless, events))
        normalized_acc.append(_gain_accuracy(normalized, events))

    stateless_mean = float(np.mean(stateless_acc))
    normalized_mean = float(np.mean(normalized_acc))
    assert stateless_mean < 0.7, "the control must actually be in the failure regime"
    assert normalized_mean >= 0.9, "normalized attribution must recover the speaker"
    assert normalized_mean - stateless_mean >= 0.35, (
        "expected a large, real margin over stateless closest-mic"
    )


def test_rolling_window_beats_static_correction_on_drifting_gain(
    gain_scenes,
) -> None:
    """The rolling window is what handles a time-varying (mid-tape step) gain.

    A single static per-source correction averages the two halves and cancels the
    step, so it is near chance; the ~15 s causal rolling level tracks the step.
    """
    static_acc, window_acc = [], []
    for scene in gain_scenes:
        segs, events = scene["segments"], scene["events"]
        static = attribute_segments(segs, scene["step_sources"])
        windowed = attribute_segments_windowed(segs, scene["step_sources"])
        static_acc.append(_gain_accuracy(static, events))
        window_acc.append(_gain_accuracy(windowed, events))

    static_mean = float(np.mean(static_acc))
    window_mean = float(np.mean(window_acc))
    assert static_mean < 0.7, "a static correction must fail across the step"
    assert window_mean >= 0.85, "the rolling window must track the step"
    assert window_mean - static_mean >= 0.3, (
        "the rolling window must beat the static correction by a real margin"
    )


def test_rolling_window_ties_static_correction_on_static_gain(gain_scenes) -> None:
    """On a *constant* imbalance the rolling window must not lose to the simple
    single static correction -- normalization, not window length, does the work."""
    static_acc, window_acc = [], []
    for scene in gain_scenes:
        segs, events = scene["segments"], scene["events"]
        static = attribute_segments(segs, scene["static_sources"])
        windowed = attribute_segments_windowed(segs, scene["static_sources"])
        static_acc.append(_gain_accuracy(static, events))
        window_acc.append(_gain_accuracy(windowed, events))

    static_mean = float(np.mean(static_acc))
    window_mean = float(np.mean(window_acc))
    assert window_mean >= 0.9
    assert abs(window_mean - static_mean) <= 0.06, (
        "window and static correction should roughly tie on a static imbalance"
    )


def test_windowed_confidence_is_emitted_and_calibrated(gain_scenes) -> None:
    """A confidence is produced for every attributed segment, and it is better
    calibrated (lower ECE) than the raw-margin confidence of stateless closest-mic
    -- raw margin is overconfident exactly on the frames the imbalance corrupts."""
    stateless_ece, window_ece = [], []
    for scene in gain_scenes:
        segs, events = scene["segments"], scene["events"]
        stateless = attribute_segments_windowed(
            segs, scene["static_sources"], gain_normalize=False
        )
        windowed = attribute_segments_windowed(segs, scene["static_sources"])

        assert all(s.confidence is not None for s in windowed)
        assert all(0.0 < (s.confidence or 0.0) <= 1.0 for s in windowed)

        stateless_ece.append(_ece(stateless, events))
        window_ece.append(_ece(windowed, events))

    # Strict per scene and in the mean: no seed is permitted to tie, so a
    # regression cannot hide inside an average.
    for stateless_e, window_e in zip(stateless_ece, window_ece):
        assert window_e < stateless_e, (
            "normalized confidence must be strictly better calibrated per seed"
        )
    assert float(np.mean(window_ece)) < float(np.mean(stateless_ece)), (
        "normalized confidence must be strictly better calibrated than raw margin"
    )


def test_single_candidate_confidence_is_degenerate(tmp_path) -> None:
    """One candidate always yields confidence ~1.0, even when it barely clears
    the floor. The gate/floor is what rejects a weak claim; the confidence is
    simply undefined between candidates. Locked so the degeneracy cannot drift."""
    audio = np.zeros(SYNTH_SR, dtype=np.float32)
    audio[: SYNTH_SR // 2] = 0.1
    path = tmp_path / "only.wav"
    sf.write(str(path), audio, SYNTH_SR)
    sources = [Source(id="only", path=str(path), label="Alice")]
    segments = [Segment(0.1, 0.4, "hi", "only", speaker="Alice")]

    got = attribute_segments_windowed(segments, sources)

    assert got[0].speaker == "Alice"
    assert got[0].confidence == pytest.approx(1.0)


def test_attribution_uses_no_pitch_cue() -> None:
    """The attribution path carries no pitch/F0 cue.

    Synthetic ground truth showed a fixed, well-calibrated F0 cue still loses
    accuracy, so the mechanism is energy-only. Guard it structurally: no
    pitch/F0 estimator is reachable from the attribution module."""
    import cr_engine.attribute as attribute_module

    names = list(vars(attribute_module))
    assert not any("pitch" in name.lower() for name in names)
    assert not any(name.lower().startswith("f0") for name in names)
    referenced = set(attribute_segments_windowed.__code__.co_names)
    assert not any(
        "pitch" in name.lower() or name.lower() in {"f0", "f0_hz"}
        for name in referenced
    )
