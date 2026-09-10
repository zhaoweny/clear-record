"""Tests for cross-talk-aware attribution and the synthetic cross-talk model."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from cr_core import Segment, Source
from cr_engine import (
    SYNTH_SR,
    attribute_segments,
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
