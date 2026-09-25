"""End-to-end tests for the non-ASR pipeline stages on synthetic audio.

These exercise ingest -> align -> reconcile -> export + calibrate_report without
requiring an ASR backend, so they run in any environment (including CI without a
GPU framework). The ASR-requiring `transcribe` stage is tested separately when a
backend is available (see the @skip conditions in test_transcriber).
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli.calibrate import calibrate_report
from clear_record.pipeline import stages
from clear_record.providers import BackendBase


def _write_tone(
    path, sr: int = 8000, seconds: float = 6.0, freq: float = 220.0
) -> None:
    # aperiodic chirp so cross-correlation alignment is unambiguous
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sig = (0.4 * np.sin(phase)).astype(np.float32)
    sf.write(str(path), sig, sr)


def _workspace(tmp_path) -> str:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=220.0)
    return str(wd)


def _peak_hz(path) -> int:
    """A staged file's dominant tone, so a test can tell whose audio it holds."""
    data, sr = sf.read(str(path))
    spec = np.abs(np.fft.rfft(data))
    return int(round(float(np.fft.rfftfreq(len(data), 1.0 / sr)[int(np.argmax(spec))])))


def test_ingest_align_reconcile_export(tmp_path) -> None:
    wd = _workspace(tmp_path)
    sources = stages.ingest(wd).sources
    assert len(sources) == 2
    alignment = stages.align(wd)
    assert alignment.reference == sources[0].id

    # Seed segments "by hand" (transcribe stage is backend-gated).
    from clear_record.core import Segment

    per_source = {
        sources[0].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[0].id,
                confidence=0.9,
            )
        ],
        sources[1].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[1].id,
                confidence=0.6,
            )
        ],
    }
    from clear_record.pipeline.workspace import Workspace

    Workspace.at(tmp_path / "rec").write_segments(
        per_source, {"backend": "none", "model": "none"}
    )

    record = stages.reconcile(wd)
    assert record.segments and record.segments[0].speaker == sources[0].label

    written = stages.export(wd)
    assert "md" in written and written["md"].exists()

    report = calibrate_report(wd)
    assert report["coverage"] is not None


def test_a_stage_failure_is_a_pipeline_error_the_cli_exits_on(tmp_path) -> None:
    """A stage refuses by raising the pipeline's own error; the command surface
    is what turns that into an exit, with the stage's own message.

    An empty workspace is the shortest such refusal and the first one a user
    meets, and the same channel serves a whole run: no stage ends the process
    itself any more.
    """
    import pytest

    from clear_record.cli import cli

    wd = tmp_path / "empty"
    wd.mkdir()

    with pytest.raises(stages.PipelineError, match="no audio files"):
        stages.ingest(str(wd))
    with pytest.raises(stages.PipelineError, match="no audio files"):
        stages.run(str(wd))

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ingest", str(wd)])
    assert str(exit_info.value.code).startswith("[ingest] no audio files found in")


#: ffmpeg is both halves of the WavPack round trip: libsndfile cannot write
#: WavPack, and it cannot read it either, so the encode below goes through
#: ffmpeg the way the decode does (``engine.audio.read_audio``'s fallback).
_FFMPEG = shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg is not installed")


@requires_ffmpeg
def test_a_wavpack_tape_is_discovered_and_decoded(tmp_path) -> None:
    """``.wv`` is a source `ingest` *finds*, not a file it walks past.

    Audacity exports WavPack, and a field tape's five-track room microphone
    arrived as ``.wv`` files: outside ``AUDIO_SUFFIXES`` they were invisible to
    `ingest`/`run` — while the same file named as an explicit input decoded
    fine — so the record lost its run's independent witness. The tape is the
    only audio left in the workspace, so discovery is the only way to it, and
    the staged audio holds the tone: the decode went through the ffmpeg
    fallback libsndfile cannot provide.
    """
    sr = 8000
    t = np.arange(int(2.0 * sr), dtype=np.float64) / sr
    wd = tmp_path / "rec"
    wd.mkdir()
    tone = (0.4 * np.sin(2 * np.pi * 330.0 * t)).astype(np.float32)
    sf.write(str(wd / "take.wav"), tone, sr)
    subprocess.run(
        [
            _FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            str(wd / "take.wav"),
            "-c:a",
            "wavpack",
            str(wd / "take.wv"),
        ],
        check=True,
    )
    (wd / "take.wav").unlink()

    sources = stages.ingest(str(wd)).sources

    assert [s.id for s in sources] == ["take"]
    assert _peak_hz(sources[0].path) == 330


def test_ingest_names_a_file_discovery_cannot_use(tmp_path) -> None:
    """A file with a suffix outside the set is *named* at ingest, not dropped.

    Discovery walked past anything it could not use and said nothing, so a tape
    in a container the set did not list left no trace at all. The line states
    the file where discovery happens — before the first decode, and before a
    discovery of nothing usable refuses — and the workspace's own state stays
    out of it: ``glossary.txt`` is the workspace's bookkeeping file and
    ``.DS_Store`` is a hidden entry, and neither is a tape the operator meant to
    hand over.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    (wd / "take.aup").write_bytes(b"<audacity project>")
    (wd / "glossary.txt").write_text("Clear Record\n", encoding="utf-8")
    (wd / ".DS_Store").write_bytes(b"\x00")

    from clear_record.core import JobEvent

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["a"]
    assert [e.message for e in events if e.message] == [
        "[ingest] cannot use take.aup: not a recognized audio file (.aup)",
        "[ingest] decode a.wav -> a.wav",
        f"[ingest] {len(sources)} source(s) -> {wd / 'manifest.json'}",
        *[f"  {source.id:24s} {source.path}" for source in sources],
    ]
    assert [
        (e.source, e.level) for e in events if e.message.startswith("[ingest] cannot")
    ] == [(None, "warn")]


def test_ingest_names_what_it_cannot_use_before_it_refuses(tmp_path) -> None:
    """A directory of nothing but unusable files still says which they were.

    The field tape's whole set was WavPack: discovery found no audio at all and
    `ingest` refused with the directory, which named nothing. The files are
    reported before that refusal, so the operator reads what was walked past
    even when the pass has nothing else to say.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    (wd / "take.aup").write_bytes(b"<audacity project>")

    from clear_record.core import JobEvent

    events: list[JobEvent] = []
    with pytest.raises(stages.PipelineError, match="no audio files"):
        stages.ingest(str(wd), on_event=events.append)

    assert [e.message for e in events if e.message] == [
        "[ingest] cannot use take.aup: not a recognized audio file (.aup)"
    ]


def test_ingest_leaves_the_apps_own_agent_drafts_unnamed(tmp_path) -> None:
    """The console's agent drafts live under ``<ws>/agent`` (ADR-0031), and that
    directory is the workspace's own state, not a tape: a pass does not narrate
    a meeting's drafts, locks or promoted minutes back at the operator.

    Only the *naming* walk is kept off it — the audio beneath it is still
    discovered, which is why it is not a :data:`SKIP_DIRS` entry.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    drafts = wd / "agent" / "draft-1"
    drafts.mkdir(parents=True)
    (drafts / "draft.json").write_text("{}", encoding="utf-8")
    (drafts / "draft.lock").touch()
    (drafts / "minutes.md").write_text("# Minutes\n", encoding="utf-8")
    _write_tone(wd / "agent" / "room.wav")

    from clear_record.core import JobEvent

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["a", "agent__room"]
    assert [e.message for e in events if e.message.startswith("[ingest] cannot")] == []


def test_reconcile_sets_title_and_markdown_h1(tmp_path) -> None:
    """The workspace directory name travels into the record metadata and becomes
    the exported H1, so a qmd index can tell recordings apart."""
    wd = _workspace(tmp_path)
    sources = stages.ingest(wd).sources
    stages.align(wd)

    # Seed segments "by hand" (transcribe stage is backend-gated).
    from clear_record.core import Segment

    per_source = {
        sources[0].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[0].id,
                confidence=0.9,
            )
        ],
    }
    from clear_record.pipeline.workspace import Workspace

    Workspace.at(tmp_path / "rec").write_segments(
        per_source, {"backend": "none", "model": "none"}
    )

    record = stages.reconcile(wd)
    assert record.metadata["title"] == "rec"

    written = stages.export(wd)
    first_line = written["md"].read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "# Record — rec"


def test_markdown_without_title_keeps_bare_h1(tmp_path) -> None:
    """Backward compatibility: a record written before titles were stored (no
    `title` metadata) still renders the bare `# Record`."""
    from clear_record.core import RecordDocument
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    Workspace.at(wd).write_record(
        RecordDocument(sources=(), alignment=None, segments=())
    )

    written = stages.export(str(wd))
    first_line = written["md"].read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "# Record"


def test_ingest_splits_multichannel_sources(tmp_path) -> None:
    """A 4-channel meeting/DJI capture becomes four per-channel sources (auto),
    and `--mix-down` collapses it to one."""
    sr = 8000
    t = np.arange(int(3.0 * sr), dtype=np.float64) / sr
    chans = [np.sin(2 * np.pi * f * t).astype(np.float32) for f in (200, 300, 400, 500)]
    quad = tmp_path / "meeting.wav"
    sf.write(str(quad), np.stack(chans, axis=1), sr)

    wd = tmp_path / "sess"
    wd.mkdir()
    (wd / "meeting.wav").unlink(missing_ok=True)
    import shutil

    shutil.copy(str(quad), str(wd / "meeting.wav"))

    split_sources = stages.ingest(str(wd), split="auto").sources
    assert len(split_sources) == 4
    assert all(s.id.endswith(("ch1", "ch2", "ch3", "ch4")) for s in split_sources)

    mixed = stages.ingest(str(wd), split="mix").sources
    assert len(mixed) == 1
    assert not mixed[0].id.endswith("ch1")


def test_ingest_gives_colliding_ids_their_own_staged_audio(tmp_path) -> None:
    """Two inputs whose ids would read alike each get their own staged file.

    ``_source_id`` folds separators to ``__``, so splitting a four-channel
    ``x.wav`` proposes ``x__ch1``… for its channels while an ordinary top-level
    ``x__ch1.wav`` proposes that very same ``x__ch1``. Before this, the second
    decode overwrote the first's ``audio/x__ch1.wav`` while the manifest kept two
    sources naming one id **and** one path: five sources staged into four files,
    and a record that looked complete. The unit that stages an id first keeps it;
    one that meets a taken id — a slug another unit proposed, or a ``-N`` id this
    pass handed out — is disambiguated, and the collision is said out loud on the
    pass's own channel.
    """
    sr = 8000
    t = np.arange(int(2.0 * sr), dtype=np.float64) / sr
    chans = [
        (0.4 * np.sin(2 * np.pi * f * t)).astype(np.float32)
        for f in (200.0, 300.0, 400.0, 500.0)
    ]
    wd = tmp_path / "rec"
    wd.mkdir()
    sf.write(str(wd / "x.wav"), np.stack(chans, axis=1), sr)
    mono = (0.4 * np.sin(2 * np.pi * 700.0 * t)).astype(np.float32)
    sf.write(str(wd / "x__ch1.wav"), mono, sr)

    from clear_record.core import JobEvent
    from clear_record.pipeline.workspace import Workspace

    events: list[JobEvent] = []
    report = stages.ingest(str(wd), on_event=events.append)

    ids = [s.id for s in report.sources]
    paths = [s.path for s in report.sources]
    # The four channels keep the ids their slugs proposed; only the fifth input,
    # whose slug the quad's first channel already holds, is disambiguated.
    assert ids == ["x__ch1", "x__ch2", "x__ch3", "x__ch4", "x__ch1-2"]
    assert len(set(paths)) == len(paths) == 5
    # Each staged file holds its own input's audio — in particular the collided
    # id's file is the mono tone, not the quad's first channel, which is what the
    # silent overwrite used to leave there.
    assert [_peak_hz(p) for p in paths] == [200, 300, 400, 500, 700]
    # The manifest a later stage reads carries the same five.
    assert [s.id for s in Workspace.at(wd).load_manifest()[0]] == ids
    # And the collision is reported, not taken silently.
    collisions = [e for e in events if e.message.startswith("[ingest] id collision")]
    assert [e.message for e in collisions] == [
        "[ingest] id collision: 'x__ch1' already taken by x.wav ch1/4; "
        "x__ch1.wav staged as 'x__ch1-2'"
    ]
    assert [(e.source, e.level) for e in collisions] == [("x__ch1-2", "warn")]


def test_ingest_holds_a_handed_out_id_against_a_later_slug(tmp_path) -> None:
    """The ids a pass hands out are held like the slugs it proposes.

    So an input can meet an id no other input's slug reads. Declared as
    ``[y__ch1.wav, y.wav (4-channel, split), y__ch1-2.wav]``, the quad's first
    channel is disambiguated onto ``y__ch1-2``, and the last input — whose own
    slug nothing else reads — stages as ``y__ch1-2-2`` rather than land on the
    channel's file. And it is one *channel* that gets renamed on a split input, so
    the line names that channel, not the whole tape.
    """
    sr = 8000
    t = np.arange(int(2.0 * sr), dtype=np.float64) / sr
    chans = [
        (0.4 * np.sin(2 * np.pi * f * t)).astype(np.float32)
        for f in (200.0, 300.0, 400.0, 500.0)
    ]
    wd = tmp_path / "rec"
    wd.mkdir()
    sf.write(str(wd / "y.wav"), np.stack(chans, axis=1), sr)
    mono = (0.4 * np.sin(2 * np.pi * 700.0 * t)).astype(np.float32)
    sf.write(str(wd / "y__ch1.wav"), mono, sr)
    sf.write(str(wd / "y__ch1-2.wav"), mono, sr)

    from clear_record.core import JobEvent

    events: list[JobEvent] = []
    report = stages.ingest(
        str(wd),
        audio_files=[
            str(wd / "y__ch1.wav"),
            str(wd / "y.wav"),
            str(wd / "y__ch1-2.wav"),
        ],
        on_event=events.append,
    )

    ids = [s.id for s in report.sources]
    assert ids == ["y__ch1", "y__ch1-2", "y__ch2", "y__ch3", "y__ch4", "y__ch1-2-2"]
    assert len({s.path for s in report.sources}) == len(ids)
    assert [
        e.message for e in events if e.message.startswith("[ingest] id collision")
    ] == [
        "[ingest] id collision: 'y__ch1' already taken by y__ch1.wav; "
        "y.wav ch1/4 staged as 'y__ch1-2'",
        "[ingest] id collision: 'y__ch1-2' already taken by y.wav ch1/4; "
        "y__ch1-2.wav staged as 'y__ch1-2-2'",
    ]


def test_diarize_hands_back_what_each_source_yielded(tmp_path) -> None:
    """`diarize` returns what the pass produced, per source: the segments it holds
    with the labels it applied, and the counts each source's line states — the
    speakers the clustering found, and the decode failure that skipped a source.

    Only that last decision is missing from the segments: a skipped source keeps
    its segments and its old labels, so nothing there tells it apart from a source
    the pass found one speaker in. Two harmonic voices an octave apart over four
    seeded segments are a split the engine decides deterministically (its own
    diarize tests pin that).
    """
    from clear_record.core import Segment
    from clear_record.pipeline.workspace import Workspace

    sr = 16000

    def harmonic(f0: float) -> np.ndarray:
        t = np.arange(sr, dtype=np.float64) / sr
        return sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6))

    wd = tmp_path / "rec"
    wd.mkdir()
    seq = [harmonic(110.0), harmonic(220.0), harmonic(110.0), harmonic(220.0)]
    voices = np.concatenate(seq)
    voices /= np.max(np.abs(voices)) or 1.0
    sf.write(str(wd / "mixed.wav"), voices.astype(np.float32), sr)
    _write_tone(wd / "broken.wav", seconds=1.0)

    ids = [s.id for s in stages.ingest(str(wd)).sources]
    assert sorted(ids) == ["broken", "mixed"]
    seeded = {
        "mixed": [
            Segment(start=float(i), end=i + 1.0, text="line", source="mixed")
            for i in range(4)
        ],
        "broken": [Segment(start=0.0, end=1.0, text="line", source="broken")],
    }
    Workspace.at(wd).write_segments(seeded, {"backend": "none", "model": "none"})
    # An unreadable source is non-fatal: it is skipped, not a failed pass.
    (wd / "audio" / "broken.wav").unlink()

    report = stages.diarize(str(wd), speakers=2)

    facts = {fact.id: fact for fact in report.sources}
    assert facts["mixed"].speakers == 2
    assert facts["mixed"].segments == 4
    assert facts["mixed"].skipped is None
    assert facts["broken"].skipped, "the unreadable source came back with its reason"
    # The relabelled segments ride beside the counts: each voice keeps one label,
    # and the two voices do not share one (which number is which is the engine's).
    labels = [seg.speaker for seg in report.per_source["mixed"]]
    assert labels[0] == labels[2] and labels[1] == labels[3] and labels[0] != labels[1]
    assert all(label and label.startswith("Speaker ") for label in labels)
    # A source that could not be decoded keeps its segments and its old labels.
    assert report.per_source["broken"] == seeded["broken"]


def test_attribute_stage_corrects_crosstalk_then_reconcile_preserves(tmp_path) -> None:
    """`attribute` re-labels bleed-dominated segments from relative energy, and
    `reconcile` keeps the corrected speaker (composability)."""
    import soundfile as sf

    from clear_record.core import Segment
    from clear_record.engine import SYNTH_SR
    from clear_record.engine.synth import make_crosstalk_scene
    from clear_record.pipeline.workspace import Workspace

    devices, events = make_crosstalk_scene(
        duration_s=16.0, n_speakers=2, bleed_db=-6.0, seed=7, non_overlapping=True
    )
    wd = tmp_path / "ct"
    wd.mkdir()
    for i, device in enumerate(devices):
        sf.write(str(wd / f"lav{i}.wav"), device, SYNTH_SR)

    sources = stages.ingest(str(wd)).sources
    labels = {s.id: s.label for s in sources}
    truth = {f"w{i}": labels[f"lav{e['speaker']}"] for i, e in enumerate(events)}
    # Every segment is taken from the bleed channel and given the naive label.
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    for i, e in enumerate(events):
        wrong = f"lav{1 - e['speaker']}"
        per_source[wrong].append(
            Segment(
                start=e["start"],
                end=e["end"],
                text=f"w{i}",
                source=wrong,
                speaker=labels[wrong],
            )
        )
    Workspace.at(wd).write_segments(per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd))
    per_source, _ = Workspace.at(wd).load_segments()
    fixed = [s for sid in per_source for s in per_source[sid]]
    assert sorted(s.speaker for s in fixed) == sorted(truth.values())

    record = stages.reconcile(str(wd))
    assert record.segments
    # Joins may merge adjacent cues, but every token keeps the corrected speaker.
    for seg in record.segments:
        for token in seg.text.split():
            assert seg.speaker == truth[token]


def test_attribute_stage_windowed_tracks_gain_and_writes_confidence(tmp_path) -> None:
    """`attribute --window-s` uses the rolling per-source level and persists the
    calibrated confidence (not just the corrected speaker), so reconcile sees it."""
    import soundfile as sf

    from clear_record.core import Segment
    from clear_record.engine import SYNTH_SR
    from clear_record.engine.synth import make_crosstalk_scene

    from clear_record.pipeline.workspace import Workspace

    devices, events = make_crosstalk_scene(
        duration_s=20.0, n_speakers=2, bleed_db=-9.0, seed=5, non_overlapping=True
    )
    gains = [10.0 ** (6.0 / 20.0), 10.0 ** (-6.0 / 20.0)]
    wd = tmp_path / "gain"
    wd.mkdir()
    for i, device in enumerate(devices):
        sf.write(
            str(wd / f"lav{i}.wav"), (device * gains[i]).astype("float32"), SYNTH_SR
        )

    sources = stages.ingest(str(wd)).sources
    labels = {s.id: s.label for s in sources}
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    for i, e in enumerate(events):
        wrong = f"lav{1 - e['speaker']}"  # the bleed-dominated channel
        per_source[wrong].append(
            Segment(e["start"], e["end"], f"w{i}", wrong, labels[wrong])
        )
    Workspace.at(wd).write_segments(per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd), window_s=15.0)

    per_source, _ = Workspace.at(wd).load_segments()
    fixed = [s for sid in per_source for s in per_source[sid]]
    assert sorted(s.speaker for s in fixed) == sorted(
        labels[f"lav{e['speaker']}"] for e in events
    )
    assert all(s.confidence is not None for s in fixed)
    assert all(0.0 < s.confidence <= 1.0 for s in fixed)


def test_attribute_stage_never_emits_room_as_speaker(tmp_path) -> None:
    """B1 regression: the mixed/room source lives in the manifest, so the CLI must
    keep it out of the candidate set -- it is a witness, never a speaker.

    Three speakers, only two lavs, and the room recorded as a manifest source.
    Without the exclusion the loud room wins the energy comparison and is emitted
    as a speaker for unmiked speech, contradicting the documented contract."""
    import numpy as np
    import soundfile as sf

    from clear_record.core import Segment
    from clear_record.engine import SYNTH_SR, mix_crosstalk
    from clear_record.engine.synth import make_speaker_stems

    from clear_record.pipeline.workspace import Workspace

    stems, events = make_speaker_stems(
        duration_s=16.0, n_speakers=3, seed=7, non_overlapping=True
    )
    wd = tmp_path / "ctroom"
    wd.mkdir()
    for i in range(2):
        sf.write(
            str(wd / f"mic{i}.wav"), mix_crosstalk(stems, i, bleed_db=-12.0), SYNTH_SR
        )
    sf.write(str(wd / "room.wav"), np.sum(stems, axis=0).astype(np.float32), SYNTH_SR)

    sources = stages.ingest(str(wd)).sources
    labels = {s.id: s.label for s in sources}
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    expected: dict[str, str] = {}
    for i, e in enumerate(events):
        if e["speaker"] < 2:  # covered: correct the bleed-dominated channel
            wrong = f"mic{1 - e['speaker']}"
            expected[f"w{i}"] = labels[f"mic{e['speaker']}"]
        else:  # unmiked: the room gate keeps the incoming speaker
            wrong = "mic1"
            expected[f"w{i}"] = labels["mic1"]
        per_source[wrong].append(
            Segment(e["start"], e["end"], f"w{i}", wrong, labels[wrong])
        )
    Workspace.at(wd).write_segments(per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd), mixed_source="room")
    per_source, _ = Workspace.at(wd).load_segments()
    emitted = [s for sid in per_source for s in per_source[sid]]

    assert labels["room"] not in {s.speaker for s in emitted}
    assert all(s.speaker != labels["room"] for s in emitted)
    assert all(s.speaker == expected[s.text] for s in emitted)


def test_merge_chunk_segments_dedupes_by_coverage() -> None:
    """A fully-covered duplicate is dropped; a boundary straddler keeps its
    unique tail; a later unique segment is kept whole."""
    from clear_record.core import Segment
    from clear_record.pipeline.transcription import merge_chunk_segments

    def seg(start: float, end: float, text: str) -> Segment:
        return Segment(start=start, end=end, text=text, source="src")

    first = [seg(0.0, 5.0, "alpha"), seg(5.0, 10.0, "beta")]
    # chunk 1 overlaps [2, 10]: "alpha" is a covered duplicate; "gamma"
    # straddles the emitted frontier; "delta" is entirely new.
    second = [
        seg(2.0, 5.0, "alpha"),
        seg(8.0, 13.0, "gamma"),
        seg(13.0, 15.0, "delta"),
    ]

    out = merge_chunk_segments([first, second])
    assert [(s.start, s.end, s.text) for s in out] == [
        (0.0, 5.0, "alpha"),
        (5.0, 10.0, "beta"),
        (10.0, 13.0, "gamma"),
        (13.0, 15.0, "delta"),
    ]
    assert out[-1].text == "delta"  # the unique tail survived
    for earlier, later in zip(out, out[1:]):
        assert earlier.end <= later.start + 1e-9


def test_align_reports_unresolved_source(tmp_path) -> None:
    """`align` names the sources it could not place instead of silently
    recording a fake zero offset."""
    import dataclasses

    from clear_record.core import JobEvent
    from clear_record.pipeline.workspace import Workspace

    wd = _workspace(tmp_path)
    sources = stages.ingest(wd).sources
    broken = [
        dataclasses.replace(s, path=str(tmp_path / "missing.wav")) if s.id == "b" else s
        for s in sources
    ]
    Workspace.at(wd).write_manifest(broken)

    events: list[JobEvent] = []
    alignment = stages.align(wd, on_event=events.append)
    assert "b" in alignment.unresolved
    # The stage reports what it found on its own channel — the same line the
    # command surface prints — and returns the typed alignment beside it.
    assert "  b                        UNRESOLVED (could not place this source)" in [
        event.message for event in events
    ]


def test_run_threads_reference_source(tmp_path, monkeypatch) -> None:
    """`run` must measure alignment against the chosen reference, not the
    first source (the stages below are stubbed so no ASR backend is needed)."""
    wd = _workspace(tmp_path)
    seen: dict[str, str] = {}
    real_align = stages.align

    def spy_align(directory, reference=None, *, on_event=None):
        alignment = real_align(directory, reference=reference, on_event=on_event)
        seen["reference"] = alignment.reference
        return alignment

    monkeypatch.setattr(stages, "align", spy_align)
    monkeypatch.setattr(stages, "transcribe", lambda *a, **k: None)
    monkeypatch.setattr(stages, "diarize", lambda *a, **k: None)
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(stages, "export", lambda *a, **k: None)

    stages.run(wd, stages.PipelineOptions(backend="fake", reference="b"))
    assert seen["reference"] == "b"


def test_run_attribute_energy_selects_energy_path(tmp_path, monkeypatch) -> None:
    """`run --attribute-energy` routes to energy attribution and leaves the
    default spectral `diarize` path untouched when the flag is off."""
    wd = _workspace(tmp_path)
    calls = {"attribute": 0, "diarize": 0}

    def spy_attribute(directory, mixed_source=None, window_s=None, *, on_event=None):
        calls["attribute"] += 1
        return stages.AttributeReport(per_source={}, segments=0, speakers=0, changed=0)

    def spy_diarize(directory, speakers=None, *, on_event=None):
        calls["diarize"] += 1

    # The transcribe stage hands `run` a report, and the attribution pass rides
    # on it (`--attribute-energy` is that stage's alternative to diarization).
    monkeypatch.setattr(
        stages,
        "transcribe",
        lambda *a, **k: stages.TranscribeReport(per_source={}, meta={}),
    )
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(stages, "export", lambda *a, **k: None)
    monkeypatch.setattr(stages, "attribute", spy_attribute)
    monkeypatch.setattr(stages, "diarize", spy_diarize)

    stages.run(
        wd,
        stages.PipelineOptions(backend="fake", attribute_energy=True, mixed_source="b"),
    )
    assert calls == {"attribute": 1, "diarize": 0}

    stages.run(wd, stages.PipelineOptions(backend="fake", do_diarize=True))
    assert calls == {"attribute": 1, "diarize": 1}


def test_run_executes_stages_in_spec_order(tmp_path, monkeypatch) -> None:
    """`run` reads its order from the spec rather than restating it, so the CLI
    subcommands and the full pipeline cannot drift apart."""
    from clear_record.core import pipeline_spec

    wd = _workspace(tmp_path)
    order: list[str] = []

    def record(name: str, impl):
        def wrapped(directory, *args, **kwargs):
            order.append(name)
            return impl(directory, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(stages, "ingest", record("ingest", stages.ingest))
    monkeypatch.setattr(stages, "align", record("align", stages.align))
    monkeypatch.setattr(
        stages, "transcribe", lambda *a, **k: order.append("transcribe")
    )
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: order.append("reconcile"))
    monkeypatch.setattr(stages, "export", lambda *a, **k: order.append("export"))
    monkeypatch.setattr(stages, "diarize", lambda *a, **k: None)

    stages.run(wd, stages.PipelineOptions(backend="fake"))

    assert order == list(pipeline_spec().cli_commands())


def test_transcribe_chunks_resume_and_glossary_invalidation(
    tmp_path, monkeypatch
) -> None:
    """Chunked transcription is resumable, and adding a glossary invalidates the
    chunk cache so a background first pass can be corrected."""
    import soundfile as sf

    from clear_record.core import Segment, TranscriptionResult
    from clear_record.providers import BackendInfo

    from clear_record.pipeline import stages

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        calls = 0
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            type(self).calls += 1
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(
                        start=0.0,
                        end=round(dur, 3),
                        text="chunk",
                        source="fake",
                        confidence=0.5,
                    ),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    first = Fake.calls
    assert first > 1  # the 8 s tape was chunked

    # resume: every chunk cached -> no backend calls
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls == first

    # a new glossary invalidates the cache -> re-transcribes
    stages.glossary(str(wd), add=["ProjectX"])
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls > first


def test_a_resumed_run_counts_reused_chunks_apart_from_decoded_ones(
    tmp_path, monkeypatch
) -> None:
    """The transcribe stage separates cached chunks from work the clock paid for.

    RUN-03 derives a live speed from exactly these events — ``(index - reused)``
    chunks of audio over the event's elapsed seconds — so a cached chunk has to
    advance the stage *as re-used*: counted as done (the progress bar is right)
    and never counted as decoded work. Without the split a resume reports a rate
    no machine sustained: the seed's own 112-of-120 reuse shape prints ~260x
    where the machine measures ~2.4x.
    """
    from clear_record.pipeline.workspace import Workspace
    from clear_record.core import JobEvent, Segment, TranscriptionResult
    from clear_record.engine.chunk import plan_chunks
    from clear_record.providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", seconds=8.0)
    sources = stages.ingest(str(wd)).sources

    class Fake(BackendBase):
        calls = 0
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            type(self).calls += 1
            data, file_sr = sf.read(audio_path)
            duration = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(0.0, round(duration, 3), "chunk", "fake", confidence=0.5),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=duration,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())
    chunk_seconds, overlap_seconds = 3.0, 1.0
    stages.transcribe(
        str(wd), "fake", chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds
    )
    n_chunks = len(plan_chunks(8.0, chunk_seconds, overlap_seconds))

    # Drop one cached body: the next run decodes exactly that chunk and re-uses
    # the rest, which is the shape a resume has.
    cache = Workspace.at(wd).chunk_cache(sources[0].id)
    bodies = sorted(cache.directory.glob("[0-9]*.json"))
    assert len(bodies) == n_chunks
    bodies[0].unlink()

    events: list[JobEvent] = []
    Fake.calls = 0
    stages.transcribe(
        str(wd),
        "fake",
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
        on_event=events.append,
    )
    last = [event for event in events if event.stage == "transcribe"][-1]

    assert Fake.calls == 1  # the chunk whose body was dropped
    assert last.index == n_chunks
    assert last.reused == n_chunks - 1
    # What a speed reading may divide by is the work the clock actually covered.
    assert last.index - last.reused == Fake.calls


def test_the_console_bar_never_steps_back_inside_a_stage(tmp_path, monkeypatch) -> None:
    """The console draws its bar — and the "N / M" beside it — from the newest
    event of a run's stream (``RunState.summary``), and a stage reports its own
    lines on that stream. A line about one source's chunk must therefore carry
    the **pass's** counters, not that source's ordinal: two bases on one stream
    make the pass read 1/4 after 1/8 and 2/8 after 2/4.

    The sequence asserted is the one the console actually reads, replayed event
    by event over a real two-source pass; the per-source facts are pinned where
    they live — the event's ``source`` and the line's own text.
    """
    from clear_record.core import JobEvent, Segment, TranscriptionResult
    from clear_record.providers import BackendInfo
    from clear_record.service import RunState

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", seconds=8.0)
    _write_tone(wd / "b.wav", seconds=8.0, freq=260.0)
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(self, audio_path, **kwargs):
            data, sample_rate = sf.read(audio_path)
            duration = len(data) / sample_rate
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, round(duration, 3), "chunk", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=duration,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())

    events: list[JobEvent] = []
    stages.transcribe(
        str(wd),
        "fake",
        chunk_seconds=3.0,
        overlap_seconds=1.0,
        jobs=1,  # the pool serial, so the recorded order is the decoded order
        on_event=events.append,
    )

    # One reading per prefix of the stream: this is what the console sees.
    readings = [
        RunState(run_id=1, meeting_id=1, events=events[:length]).summary()
        for length in range(1, len(events) + 1)
    ]
    assert readings, "the pass reported nothing"

    indices = [reading.index for reading in readings]
    assert indices == sorted(indices), [(r.stage, r.index, r.total) for r in readings]
    # One unit base for the pass: two sources of four chunks each is 8, and no
    # line may state a base of its own (2 for the plan, 4 for a source's chunk).
    assert {reading.total for reading in readings} == {8}, indices

    # The per-source facts a reader of one line wants are on the line.
    chunk_lines = [
        event for event in events if event.message.startswith("[transcribe]   ")
    ]
    assert [event.source for event in chunk_lines] == ["a"] * 4 + ["b"] * 4
    assert "chunk 1/4 [0-3s] -> 1 segment(s)" in chunk_lines[0].message
    assert "chunk 4/4 [6-8s] -> 1 segment(s)" in chunk_lines[-1].message


def test_transcription_module_is_a_single_seam(tmp_path) -> None:
    """The resumable transcriber is callable through one function — plan ->
    cache -> pool -> merge — driven by a `Workspace` and plain data types, with
    no private helper in sight."""
    from clear_record.core import Segment, Source, TranscriptionResult
    from clear_record.providers import BackendInfo

    from clear_record.pipeline.transcription import TranscriptionOptions, transcribe
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    wav = wd / "a.wav"
    sf.write(str(wav), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr)

    class Fake:
        calls = 0
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def transcribe(self, audio_path, **kwargs):
            type(self).calls += 1
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, round(dur, 3), "chunk", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    backend = Fake()
    source = Source(id="a", path=str(wav))
    options = TranscriptionOptions(chunk_seconds=3.0, overlap_seconds=1.0, jobs=2)
    result = transcribe([source], backend, options, workspace=Workspace.at(wd))

    assert result.per_source["a"]
    assert result.source_meta["a"]["chunks"] > 1
    assert result.jobs == 2
    assert result.model == "fake"
    first = Fake.calls

    # Resume through the same seam: cached chunks mean no new backend calls.
    again = transcribe(
        [source],
        backend,
        TranscriptionOptions(chunk_seconds=3.0, overlap_seconds=1.0, jobs=2),
        workspace=Workspace.at(wd),
    )
    assert again.per_source["a"]
    assert Fake.calls == first


def test_resolve_jobs_is_adaptive() -> None:
    from clear_record.pipeline.transcription import resolve_jobs

    assert resolve_jobs(False, 10, 4) == 1  # in-process -> serialized
    assert resolve_jobs(True, 1, 4) == 1  # no work to parallelize
    assert resolve_jobs(True, 10, 2) == 2  # explicit request wins
    assert 1 <= resolve_jobs(True, 10, 0) <= 4  # bounded adaptive default


def test_transcribe_runs_pending_chunks_concurrently(tmp_path, monkeypatch) -> None:
    """A process-isolated backend's pending chunks run overlapped, and the
    merged result is still per-source complete."""
    import threading
    import time

    from clear_record.pipeline import stages
    from clear_record.core import Segment, TranscriptionResult
    from clear_record.providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                data, file_sr = sf.read(audio_path)
                dur = len(data) / file_sr
                return TranscriptionResult(
                    source="fake",
                    segments=(
                        Segment(
                            start=0.0,
                            end=round(dur, 3),
                            text="chunk",
                            source="fake",
                            confidence=0.5,
                        ),
                    ),
                    language="en",
                    backend="fake",
                    model="fake",
                    audio_duration=dur,
                )
            finally:
                with self.lock:
                    self.active -= 1

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    report = stages.transcribe(
        str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=4
    )
    assert fake.max_active > 1  # chunks actually overlapped
    assert report.per_source["a"]  # and the merged output is complete


def test_transcribe_resolves_model_once_before_the_pool(tmp_path, monkeypatch) -> None:
    """The first-use model resolve/download runs once on the main thread, before
    any worker: a parallel backend must never race it (the provider also makes
    the download itself single-flight)."""
    import threading
    import time

    from clear_record.pipeline import stages
    from clear_record.core import Segment, TranscriptionResult
    from clear_record.providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    events: list[str] = []

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def prepare(self, model, model_dir):
            events.append(f"resolve:{threading.current_thread().name}")
            return "/tmp/fake-model.bin"

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            events.append(f"transcribe:{threading.current_thread().name}")
            time.sleep(0.02)
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(
                        start=0.0,
                        end=round(dur, 3),
                        text="chunk",
                        source="fake",
                        confidence=0.5,
                    ),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    stages.transcribe(str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=4)

    # Resolved exactly once, on the main thread, before any worker ran.
    assert events[0] == "resolve:MainThread"
    assert sum(e.startswith("resolve:") for e in events) == 1
    # ...and the pool really did fan out, so this exercises the parallel path.
    assert any(
        e.startswith("transcribe:") and e != "transcribe:MainThread" for e in events
    )


def test_auto_jobs_is_capped_by_model_and_vram_and_overridable(monkeypatch) -> None:
    """The `jobs=0` default is bounded by the model's resident size against the
    GPU memory, so N large models cannot OOM the documented minimum GPU; an
    explicit `--jobs` / `CR_JOBS` still wins."""
    import os

    from clear_record.pipeline.transcription import (
        auto_jobs,
        detect_vram_gb,
        model_vram_gb,
        resolve_jobs,
    )

    monkeypatch.setattr(os, "cpu_count", lambda: 16)

    # A large model cannot fan out on a small (8 GB) GPU; a medium one gets a
    # few; the 4-way fan-out cap still applies on a big (24 GB) GPU.
    assert auto_jobs(10, "large-v3", 8.0) == 1
    assert auto_jobs(10, "medium", 8.0) == 3
    assert auto_jobs(10, "large-v3", 24.0) == 4
    assert auto_jobs(10, "small", 24.0) == 4
    # Unknown / missing models are assumed large, which can only lower the cap.
    assert auto_jobs(10, None, 8.0) == 1

    # Name parsing covers filenames, paths and quantization suffixes.
    assert model_vram_gb("ggml-large-v3.bin") == 3.7
    assert model_vram_gb("/models/ggml-large-v3-q5_0.bin") == 3.7
    assert model_vram_gb("medium") == 2.1
    assert model_vram_gb("mystery") == 3.7

    # CR_VRAM_GB overrides the (hardware-dependent) probe for the auto path...
    monkeypatch.setenv("CR_VRAM_GB", "24")
    assert detect_vram_gb() == 24.0
    assert resolve_jobs(True, 10, 0, model="large-v3") == 4
    # ...but explicit --jobs and CR_JOBS override the advisory cap.
    assert resolve_jobs(True, 10, 6, model="large-v3", vram_gb=8.0) == 6
    monkeypatch.setenv("CR_JOBS", "5")
    assert resolve_jobs(True, 10, 0, model="large-v3", vram_gb=8.0) == 5


def test_transcribe_interrupt_cancels_queue_and_kills_children(
    tmp_path, monkeypatch
) -> None:
    """Ctrl-C cancels queued chunks and terminates the in-flight children
    promptly, leaving a consistent, resumable chunk cache.

    The backend is a hardware-free stub that spawns a real, killable child
    process (a stand-in for `whisper-cli`), so this exercises the pool's real
    process-termination path without a GPU or model weights.
    """
    import json
    import os
    import signal
    import subprocess
    import sys
    import threading
    import time

    import pytest

    from clear_record.pipeline.workspace import Workspace
    from clear_record.core import Segment, TranscriptionResult
    from clear_record.engine import plan_chunks
    from clear_record.providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(10 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    # The pool must not monkey-patch the global Popen (ticket 08).
    original_popen_init = subprocess.Popen.__init__

    started = threading.Event()
    lock = threading.Lock()
    procs: list[subprocess.CompletedProcess] = []

    class BlockingFake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            from clear_record.core.process import SubprocessRunner

            runner = process_runner or SubprocessRunner()
            started.set()
            # A real child process stands in for `whisper-cli`, launched through
            # the pool's injected runner (no global Popen patch): the pool must
            # find and terminate it, not merely abandon the worker thread.
            result = runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                text=True,
            )
            with lock:
                procs.append(result)
            if result.returncode != 0:
                raise RuntimeError(f"whisper-cli killed (exit {result.returncode})")
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: BlockingFake())

    def interrupt_once() -> None:
        started.wait(5)
        time.sleep(0.2)  # let both workers spawn while the rest queue
        os.kill(os.getpid(), signal.SIGINT)

    helper = threading.Thread(target=interrupt_once, daemon=True)
    helper.start()
    began = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        stages.transcribe(
            str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=2
        )
    elapsed = time.monotonic() - began
    helper.join(timeout=2)

    # No global patch was installed (or left behind) by the pool.
    assert subprocess.Popen.__init__ is original_popen_init

    # Bounded: it did not block on the 30 s children.
    assert elapsed < 5.0
    # The in-flight children were terminated and queued chunks never ran.
    assert procs, "at least one chunk should have started"
    for proc in procs:
        assert proc.returncode != 0
    n_chunks = len(plan_chunks(10.0, 2.0, 0.5))
    assert len(procs) < n_chunks

    # Cache stays consistent and resumable: meta intact, every body complete,
    # no half-written temp files.
    cache = Workspace.at(wd).chunk_cache("a")
    meta = json.loads(cache.meta_path.read_text(encoding="utf-8"))
    assert meta["n_chunks"] == n_chunks
    cached: list[int] = []
    for body in cache.directory.glob("*.json"):
        data = json.loads(body.read_text(encoding="utf-8"))
        if body.name != "_meta.json":
            assert isinstance(data, list)
            cached.append(int(body.stem))
    assert set(cached) <= set(range(n_chunks))
    assert not list(cache.directory.glob("*.tmp"))

    # Resuming completes the plan, recomputing only the uncached chunks.
    class FastFake(BlockingFake):
        calls = 0

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            type(self).calls += 1
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    fast = FastFake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fast)
    stages.transcribe(str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=2)
    assert FastFake.calls == n_chunks - len(cached)
    assert len(list(cache.directory.glob("[0-9]*.json"))) == n_chunks


def test_transcribe_check_plugin_gates_on_the_load_probe(tmp_path, monkeypatch) -> None:
    """`check_plugin=True` runs the opt-in probe once and refuses to transcribe
    when the ggml plugin fails to load. The default path never probes (the
    surface test pins the opt-in flag)."""
    import pytest

    from clear_record.providers import BackendInfo, PluginLoadProbe

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())
    calls: list = []

    def fake_probe(backend):
        calls.append(backend)
        return PluginLoadProbe(False, "a non-matching backend loaded: cuda")

    monkeypatch.setattr(stages, "probe_ggml_plugin_load", fake_probe)

    with pytest.raises(stages.PipelineError, match="did not load"):
        stages.transcribe(str(wd), "fake", check_plugin=True)
    assert len(calls) == 1, "the probe must run exactly once per invocation"


def test_no_global_popen_patch_is_left_behind() -> None:
    """Regression for ticket 08: the pool must not monkey-patch ``Popen``."""
    for name in (
        "_ORIGINAL_POPEN_INIT",
        "_tracked_popen_init",
        "_track_subprocesses",
        "_SubprocessTracker",
        "_run_tracked",
        "_PoolCancel",
    ):
        assert not hasattr(stages, name), f"stale global patch helper: {name}"


def test_two_concurrent_pools_use_distinct_runners(tmp_path, monkeypatch) -> None:
    """Two pools in one process each get their own runner, so cancellation stays
    scoped; both complete with independent caches."""
    import threading

    from clear_record.pipeline.workspace import Workspace
    from clear_record.core import Segment, TranscriptionResult
    from clear_record.providers import BackendInfo

    def make_ws(name: str) -> str:
        wd = tmp_path / name
        wd.mkdir()
        sr = 8000
        t = np.arange(sr, dtype=np.float64) / sr
        sf.write(
            str(wd / "a.wav"),
            (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32),
            sr,
        )
        stages.ingest(str(wd), split="mix")
        return str(wd)

    wd1, wd2 = make_ws("one"), make_ws("two")
    runners: list = []
    lock = threading.Lock()

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            with lock:
                runners.append(process_runner)
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())
    errors: list = []

    def worker(wd: str) -> None:
        try:
            stages.transcribe(wd, "fake", jobs=1)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(wd,)) for wd in (wd1, wd2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(runners) == 2
    assert runners[0] is not runners[1], "each pool needs its own runner"
    for wd in (wd1, wd2):
        per_source, _ = Workspace.at(wd).load_segments()
        assert per_source
