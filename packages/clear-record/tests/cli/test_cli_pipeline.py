"""End-to-end tests for the non-ASR pipeline stages on synthetic audio.

These exercise ingest -> align -> reconcile -> export + calibrate_report without
requiring an ASR backend, so they run in any environment (including CI without a
GPU framework). The ASR-requiring `transcribe` stage is tested separately when a
backend is available (see the @skip conditions in test_transcriber).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli.calibrate import calibrate_report
from clear_record.pipeline import stages
from clear_record.providers import BackendBase


def _write_tone(
    path,
    sr: int = 8000,
    seconds: float = 6.0,
    freq: float = 220.0,
    gain: float = 0.4,
) -> None:
    """One device's recording of the chirp.

    A second source of the *same* scene is the same chirp at another ``gain``:
    two recordings correlate, so ``align`` places them, while their bytes differ
    — ingest folds byte-identical inputs into one source (ticket 218), so a
    fixture writing one waveform twice would describe one device, twice.
    """
    # aperiodic chirp so cross-correlation alignment is unambiguous
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sig = (gain * np.sin(phase)).astype(np.float32)
    sf.write(str(path), sig, sr)


def _workspace(tmp_path) -> str:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=220.0, gain=0.25)
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
    _write_tone(wd / "agent" / "room.wav", gain=0.25)

    from clear_record.core import JobEvent

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["a", "agent__room"]
    assert [e.message for e in events if e.message.startswith("[ingest] cannot")] == []


def test_ingest_excludes_an_input_the_workspace_names(tmp_path) -> None:
    """A recording folder accumulates earlier sessions, so the folder carries the
    one declaration of which of its files are **not** today's.

    ``.clear-record-ignore`` is that declaration: one glob per line, matched
    against the path under the workspace root, with blank lines and ``#``
    comments declaring nothing. Discovery takes the named inputs out of the walk
    — for `ingest` and for `run` alike, since both walk through it — and *names*
    what it took out, because an exclusion nobody can see is the silent drop
    this walk exists to end.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=330.0)
    (wd / "old").mkdir()
    _write_tone(wd / "old" / "b.wav", freq=440.0)
    (wd / ".clear-record-ignore").write_text(
        "# yesterday's session, and the whole copied-in folder\nb.wav\nold/*\n",
        encoding="utf-8",
    )

    from clear_record.core import JobEvent
    from clear_record.pipeline.workspace import discover_audio

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["a"]
    # The walk the run path resolves a directory's sources through honours the
    # same declaration, so the two surfaces cannot disagree about what is in
    # the folder.
    assert [p.name for p in discover_audio(wd)] == ["a.wav"]
    excluded = [e for e in events if "excluded" in (e.message or "")]
    assert [e.message for e in excluded] == [
        "[ingest] excluded b.wav: named in .clear-record-ignore",
        "[ingest] excluded old/b.wav: named in .clear-record-ignore",
    ]
    assert [(e.level, e.source) for e in excluded] == [("info", None), ("info", None)]


def test_ingest_folds_a_byte_identical_copy_into_one_source(tmp_path) -> None:
    """The acceptance on one real workspace: a copy is not a second session, and
    the fold is reported, never silent.

    A recording folder accumulates copies — ``cp take.wav take-copy.wav`` — and a
    copied-in case brings whole earlier sessions again: on one field folder 39
    files became 39 sources, several of them md5-identical pairs, so the same
    audio was decoded, transcribed and merged twice and the record was built from
    three sessions at once. (An *editor's* twin is not this case: a DJI export's
    ``_edit`` beside its ``_orig`` is processed, so it is not byte-identical and
    is never folded — a folder leaves that out through its declaration.) The
    workspace here is a synthesised scene (``synth``) plus the copies an
    operator's folder would hold: the copy of today's take, and an earlier
    session its declaration excludes.

    The fold keeps the first input in discovery order, and the copy's ``-`` sorts
    before the take's ``.``, so the copy is the source and the take folds into it;
    the line names both files either way.
    """
    from clear_record.cli.synth import synth
    from clear_record.core import JobEvent

    wd = tmp_path / "rec"
    wd.mkdir()
    synth(str(wd), devices=2, duration_s=2.0, speakers=2)
    # The devices land under the workspace's own ``audio/`` (derived output,
    # never discovered): the take and yesterday's session are named beside it.
    shutil.copyfile(wd / "audio" / "device_0.wav", wd / "take.wav")
    shutil.copyfile(wd / "audio" / "device_1.wav", wd / "take_old.wav")
    shutil.copyfile(wd / "take.wav", wd / "take-copy.wav")
    (wd / ".clear-record-ignore").write_text("take_old.wav\n", encoding="utf-8")

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["take-copy"]
    assert [e.message for e in events if e.message] == [
        "[ingest] excluded take_old.wav: named in .clear-record-ignore",
        "[ingest] duplicate of take-copy.wav: take.wav is byte-identical, "
        "folded into one source",
        "[ingest] decode take-copy.wav -> take-copy.wav",
        f"[ingest] 1 source(s) -> {wd / 'manifest.json'}",
        f"  {'take-copy':24s} {sources[0].path}",
    ]
    assert [
        (e.level, e.source)
        for e in events
        if e.message and e.message.startswith("[ingest] ")
    ] == [("info", None), ("warn", None), ("info", "take-copy"), ("info", None)]


def _write_noise(path, sr: int = 8000, seconds: float = 6.0, seed: int = 11) -> None:
    """A tape from another session: noise shares nothing with the chirp, so
    ``align`` cannot place it (the align suite's own unplaceable fixture)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    sf.write(str(path), (0.2 * rng.standard_normal(n)).astype(np.float32), sr)


def test_reconcile_leaves_out_a_tape_align_could_not_place(tmp_path) -> None:
    """Ticket 220, on the ticket's reproduction: two unrelated tapes in one
    workspace, with the segments seeded by hand so no backend is needed.

    ``align`` reports the noise tape UNRESOLVED and writes no offset for it; the
    record writer then read that missing offset as 0.0 and merged the stray
    tape's segments in as if they belonged at the reference's start. Now they are
    left out, and ``record.json`` carries the drop summary the alignment's own
    ``unresolved`` id list did not — the source, and how much of it went with it.
    """
    from clear_record.core import Segment
    from clear_record.pipeline.workspace import Workspace, load_json

    wd = tmp_path / "sess"
    wd.mkdir()
    _write_tone(wd / "meeting.wav")
    _write_noise(wd / "stray.wav")
    sources = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))

    stray = sources[-1].id
    assert alignment.unresolved == (stray,), "the fixture must be unplaceable"
    assert stray not in alignment.offsets

    per_source = {
        sources[0].id: [
            Segment(
                start=0.0,
                end=5.0,
                text="the reference recording speaks here",
                source=sources[0].id,
                confidence=0.9,
            )
        ],
        stray: [
            Segment(
                start=10.0,
                end=15.0,
                text="an unrelated tape that could not be placed",
                source=stray,
                confidence=0.9,
            )
        ],
    }
    Workspace.at(wd).write_segments(per_source, {"backend": "none", "model": "none"})

    record = stages.reconcile(str(wd))

    assert [s.text for s in record.segments] == ["the reference recording speaks here"]
    assert record.metadata["unplaced"] == [
        {"id": stray, "segments": 1, "speech_s": 5.0}
    ]
    # The artifact a reader opens carries it, not only the stages' lines.
    written = load_json(Workspace.at(wd).record_path)
    assert written["metadata"]["unplaced"] == record.metadata["unplaced"]
    assert [seg["text"] for seg in written["segments"]] == [
        "the reference recording speaks here"
    ]


def test_reconcile_names_a_null_offset_source_it_drops(tmp_path) -> None:
    """The same silent drop, reached through a manifest rather than a dataclass.

    A hand-written or foreign manifest can carry ``"stray": null`` in
    ``offsets``. The key is present, so the drop summary's membership test called
    that source placed while the placement lookup dropped everything it held: its
    segments left the record with ``metadata.unplaced == []`` and nothing on the
    channel, which is exactly the silence this ticket exists to end.
    """
    from clear_record.core import JobEvent, Segment, load_json, write_json
    from clear_record.pipeline.workspace import Workspace

    wd = _workspace(tmp_path)
    sources = stages.ingest(wd).sources
    stages.align(wd)

    ws = Workspace.at(wd)
    manifest = load_json(ws.manifest_path)
    manifest["alignment"]["offsets"] = {sources[0].id: 0.0, sources[1].id: None}
    manifest["alignment"]["unresolved"] = []
    write_json(ws.manifest_path, manifest)
    ws.write_segments(
        {
            sources[0].id: [
                Segment(0.0, 1.5, "kept line", sources[0].id, confidence=0.9)
            ],
            sources[1].id: [
                Segment(0.0, 2.5, "dropped line", sources[1].id, confidence=0.6)
            ],
        },
        {"backend": "none", "model": "none"},
    )

    events: list[JobEvent] = []
    record = stages.reconcile(wd, on_event=events.append)

    assert [s.text for s in record.segments] == ["kept line"]
    assert record.metadata["unplaced"] == [
        {"id": sources[1].id, "segments": 1, "speech_s": 2.5}
    ]
    # The record is not the only place it is said: the channel carries the row.
    assert [e.message for e in events if e.message and "UNPLACED" in e.message] == [
        f"  {sources[1].id:24s} UNPLACED (no alignment offset)"
    ]
    assert (
        load_json(ws.record_path)["metadata"]["unplaced"] == record.metadata["unplaced"]
    )


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
    # Both tapes were placed, so nothing was left out — and the artifact says so.
    assert record.metadata["unplaced"] == []

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


def test_a_pre_roll_export_keeps_every_cue(tmp_path) -> None:
    """A source started before the reference keeps its negative reference times,
    and the exports say so rather than folding them onto 00:00:00.

    `reconcile` shifts every source onto the reference clock, so a tape whose
    phone was started first holds cues before zero (measured: -114.365 s on a
    field tape). Both formatters clamped, so those cues came out as
    `00:00:00.000 --> 00:00:00.000` — zero-length — and as
    `[00:00:00.000-00:00:00.000]` headers. The text formats keep the clock, sign
    and all; the subtitle timecode has no sign to carry, so its timeline is
    translated by the pre-roll and every cue keeps its length.
    """
    import json

    from clear_record.core import RecordDocument, Segment
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    Workspace.at(wd).write_record(
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(
                Segment(
                    start=-114.365,
                    end=-112.0,
                    text="before the clock",
                    source="phone",
                ),
                Segment(start=1.0, end=3.5, text="after it", source="room"),
            ),
        )
    )

    written = stages.export(str(wd))

    md = written["md"].read_text(encoding="utf-8")
    assert "[-00:01:54.365–-00:01:52.000] phone" in md

    srt = written["srt"].read_text(encoding="utf-8")
    assert srt.splitlines()[:3] == [
        "1",
        "00:00:00,000 --> 00:00:02,365",
        "before the clock",
    ]
    assert "00:00:00,000 --> 00:00:00,000" not in srt
    assert "00:01:55,365 --> 00:01:57,865" in srt

    vtt = written["vtt"].read_text(encoding="utf-8")
    assert "00:00:00.000 --> 00:00:02.365" in vtt
    assert "00:00:00.000 --> 00:00:00.000" not in vtt
    assert "00:01:55.365 --> 00:01:57.865" in vtt

    # The machine artifact keeps the record's own clock: the pre-roll is real.
    exported = json.loads(written["json"].read_text(encoding="utf-8"))
    assert exported["segments"][0]["start"] == -114.365


def test_a_record_that_starts_after_zero_keeps_its_times(tmp_path) -> None:
    """Only a pre-roll moves the subtitle timeline; a record whose first cue is
    already at or after zero is exported at the times it holds."""
    from clear_record.core import RecordDocument, Segment
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    Workspace.at(wd).write_record(
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(Segment(start=5.0, end=7.0, text="later", source="room"),),
        )
    )

    written = stages.export(str(wd))
    srt = written["srt"].read_text(encoding="utf-8")
    md = written["md"].read_text(encoding="utf-8")

    assert "00:00:05,000 --> 00:00:07,000" in srt
    assert "[00:00:05.000–00:00:07.000] room" in md


def test_a_time_just_before_zero_is_not_signed() -> None:
    """The last half-millisecond before zero is zero, not a signed zero.

    `reconcile` rounds to 4 decimals and the aligner's offsets are whole
    samples, so a source placed one sample early whose first cue starts at local
    0.0 lands on -0.0001 s: rendering that as ``-00:00:00.000`` would be a sign
    that is not real, and one the subtitles' plain ``00:00:00,000`` would not
    share. The sign comes from the millisecond the time prints as, so a *real*
    sub-millisecond negative keeps it.
    """
    from clear_record.pipeline.stages import format_timestamp

    assert format_timestamp(-0.0001) == "00:00:00.000"
    assert format_timestamp(-0.0006) == "-00:00:00.001"


def test_a_cue_a_hair_short_of_a_minute_reads_as_the_minute(tmp_path) -> None:
    """``00:01:00,000``, not ``00:00:60,000`` — no player reads the latter.

    The subtitle renderer rounds a cue to the millisecond it prints before
    splitting it into fields, the same rule `format_timestamp` follows, so a
    second past 59.9999 s carries into the minute on both surfaces instead of
    leaving a timecode whose seconds field runs to 60.
    """
    from clear_record.core import RecordDocument, Segment
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    Workspace.at(wd).write_record(
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(Segment(start=59.9999, end=61.0, text="edge", source="a"),),
        )
    )

    written = stages.export(str(wd))
    srt = written["srt"].read_text(encoding="utf-8")

    assert "00:01:00,000 --> 00:01:01,000" in srt
    assert "00:00:60" not in srt


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
    # A different tone: the two files are one id-collision fixture, and ingest
    # folds byte-identical inputs into one source (ticket 218).
    other = (0.4 * np.sin(2 * np.pi * 900.0 * t)).astype(np.float32)
    sf.write(str(wd / "y__ch1-2.wav"), other, sr)

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


#: The ticket's split-capture shape: one long `synth` scene cut into the parts a
#: recorder that rotates hands over. The parts are 200 s because a gap that wide
#: is outside the estimator's whole search band (`_MAX_LAG_S`, 120 s) — the shape
#: the field tape failed on, and the one placed without asking the audio at all
#: (a split *inside* the band is the narrow-declaration test's).
_SPLIT_TAKE_S = 400.0
_SPLIT_PART_S = _SPLIT_TAKE_S / 2.0

#: A take split the same way but inside the estimator's own search band: 90 s
#: parts 90 s apart. On the reviewer's repro — the `synth` verb's degraded 400 s
#: device (seed 7) cut at 0-90 / 90-180 and named by its start — the audio placed
#: the pair at +21.3330 s, conf 0.505, against a true +90 s: two unrelated
#: passages matching. 90 s of recording 90 s apart cannot overlap, so the
#: declaration places them (`_cannot_overlap`).
_SHORT_SPLIT_TAKE_S = 180.0

#: Two *simultaneous* stamped devices: long enough that their 30 s name gap is
#: far inside both recordings, so the audio — which measures the arming skew a
#: whole-second name cannot — is the evidence that counts.
_OVERLAP_TAKE_S = 60.0


@pytest.fixture(scope="module")
def split_take() -> tuple[np.ndarray, np.ndarray]:
    """One capture as two *sequential* parts of one recorder.

    Built straight from ``engine.synth`` (the ticket's repro cuts a ``synth``
    scene with ffmpeg; this is the same two halves without a subprocess). The
    parts share no passage — the join is a rotation, not an overlap — which is
    what makes them unreachable by correlation and reachable only by the start
    time each name states.
    """
    from clear_record.engine import make_scene

    scene, _ = make_scene(_SPLIT_TAKE_S, 2, seed=7)
    half = scene.size // 2
    return scene[:half], scene[half:]


def test_ingest_reads_a_split_recorders_starts_off_its_names(
    tmp_path, split_take
) -> None:
    """A recorder that splits one capture into numbered files hands `ingest` files
    that are *sequential*, not simultaneous (ticket 221).

    The start written into each name is what places them. The parts share no
    passage, so no correlation could ever find the 200 s between them: it is
    further than the estimator's whole search band, and the pair used to come
    back UNRESOLVED — or worse, accepted at a spurious small offset — with
    `reconcile` then stacking the two halves into one window.
    """
    from clear_record.core import JobEvent
    from clear_record.engine import SYNTH_SR
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    for name, part in (
        ("REC_20260101_120000.wav", split_take[0]),
        ("REC_20260101_120320.wav", split_take[1]),
    ):
        sf.write(str(wd / name), part, SYNTH_SR)

    events: list[JobEvent] = []
    first, second = stages.ingest(str(wd), on_event=events.append).sources

    alignment = stages.align(str(wd))
    # Placed from the declaration, and never reported unplaceable: a declared
    # start is a placement, not an estimate.
    assert alignment.unresolved == ()
    assert alignment.offsets[second.id] == pytest.approx(_SPLIT_PART_S)
    assert alignment.method == "declared-start"

    # ... and the declaration travelled: into the manifest, and onto the channel.
    assert first.start_s is not None and second.start_s is not None
    assert second.start_s - first.start_s == pytest.approx(_SPLIT_PART_S)
    assert (
        "[ingest] REC_20260101_120320.wav declares start 2026-01-01 12:03:20 "
        "(from its filename): carried into the manifest"
    ) in [event.message for event in events]
    reopened, _ = Workspace.at(wd).load_manifest()
    assert reopened[1].start_s == second.start_s

    # The field symptom itself: `reconcile` tiles the two halves end to end
    # instead of stacking them into one window (the ticket's five-way collage).
    from clear_record.core import Segment

    Workspace.at(wd).write_segments(
        {
            source.id: [Segment(start=0.0, end=1.0, text=source.id, source=source.id)]
            for source in (first, second)
        },
        {"backend": "none", "model": "none"},
    )
    assert [seg.start for seg in stages.reconcile(str(wd)).segments] == [
        0.0,
        pytest.approx(_SPLIT_PART_S),
    ]


def test_align_places_a_start_the_manifest_declares(tmp_path, split_take) -> None:
    """The ticket's repro names — `rec-A.wav`, `rec-B.wav` — state no start time,
    so the operator declares it where the record can carry it: ``start_s`` in the
    manifest. `align` places the pair from that declaration, the only evidence
    that reaches across a gap wider than its band — and keeps placing it when the
    named reference does not exist and the first source stands in (its declared
    start stands in with it).
    """
    import json
    from datetime import datetime, timezone

    from clear_record.engine import SYNTH_SR
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    for name, part in (("rec-A.wav", split_take[0]), ("rec-B.wav", split_take[1])):
        sf.write(str(wd / name), part, SYNTH_SR)

    sources = stages.ingest(str(wd)).sources
    # the names state no start time, so nothing was declared from them
    assert [source.id for source in sources] == ["rec-A", "rec-B"]

    # The declaration a user may make by hand: this file began at 12:00:00, that
    # one 200 s later.
    w = Workspace.at(wd)
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    manifest = json.loads(w.manifest_path.read_text(encoding="utf-8"))
    declared = {sources[0].id: noon, sources[1].id: noon + _SPLIT_PART_S}
    for entry in manifest["sources"]:
        entry["start_s"] = declared[entry["id"]]
    w.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.offsets[sources[1].id] == pytest.approx(_SPLIT_PART_S)

    # --reference <a name no source has>: the first source stands in (its path
    # *and* its declared start together), so the declarations are still honoured
    # rather than silently dropped.
    fallback = stages.align(str(wd), reference="nosuch")
    assert fallback.unresolved == ()
    assert fallback.offsets[sources[1].id] == pytest.approx(_SPLIT_PART_S)


def test_ingest_keeps_a_start_declared_in_the_manifest(tmp_path, split_take) -> None:
    """This pass rebuilds the manifest it reads, and `run` ingests every time, so
    a declaration has to survive the re-ingest: a source whose *name* declares
    nothing keeps the start the manifest held for its id — named on the sink,
    because a start that appears in the record without a word is how an operator
    loses track of which parts are placed by a declaration."""
    import json
    from datetime import datetime, timezone

    from clear_record.core import JobEvent
    from clear_record.engine import SYNTH_SR
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    for name, part in (("rec-A.wav", split_take[0]), ("rec-B.wav", split_take[1])):
        sf.write(str(wd / name), part, SYNTH_SR)

    w = Workspace.at(wd)
    sources = stages.ingest(str(wd)).sources
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    manifest = json.loads(w.manifest_path.read_text(encoding="utf-8"))
    declared = {sources[0].id: noon, sources[1].id: noon + _SPLIT_PART_S}
    for entry in manifest["sources"]:
        entry["start_s"] = declared[entry["id"]]
    w.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    events: list[JobEvent] = []
    again = stages.ingest(str(wd), on_event=events.append).sources
    assert [source.start_s for source in again] == [
        pytest.approx(noon),
        pytest.approx(noon + _SPLIT_PART_S),
    ]
    assert (
        "[ingest] rec-B.wav keeps declared start 2026-01-01 12:03:20: "
        "declared in the manifest"
    ) in [event.message for event in events]
    assert stages.align(str(wd)).offsets[sources[1].id] == pytest.approx(_SPLIT_PART_S)


def test_ingest_keeps_a_declared_start_across_a_fold(tmp_path, split_take) -> None:
    """A byte-identical copy is the same audio, so a declaration the manifest
    holds for the copy's id is one about the surviving source (ticket 218's fold
    meets ticket 221's carry).

    The copy sorts before the file it copies (``rec-A-copy.wav`` <
    ``rec-A.wav``), so the fold makes it the source — and the id the operator's
    hand-declared start lives under is the one that leaves the manifest. The
    declaration follows the audio: the survivor keeps the start and `align`
    places the pair from the declarations. Left to the audio the pair is
    unplaceable — the parts share no passage, which is the field symptom 221
    exists for.
    """
    import json
    from datetime import datetime, timezone

    from clear_record.core import JobEvent
    from clear_record.engine import SYNTH_SR
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    for name, part in (("rec-A.wav", split_take[0]), ("rec-B.wav", split_take[1])):
        sf.write(str(wd / name), part, SYNTH_SR)

    w = Workspace.at(wd)
    sources = stages.ingest(str(wd)).sources
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    manifest = json.loads(w.manifest_path.read_text(encoding="utf-8"))
    declared = {sources[0].id: noon, sources[1].id: noon + _SPLIT_PART_S}
    for entry in manifest["sources"]:
        entry["start_s"] = declared[entry["id"]]
    w.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # The operator's folder gains the copy, and discovery order hands it the
    # source: the declaration on ``rec-A`` is the one that has to travel.
    shutil.copyfile(wd / "rec-A.wav", wd / "rec-A-copy.wav")

    events: list[JobEvent] = []
    again = stages.ingest(str(wd), on_event=events.append).sources
    assert [source.id for source in again] == ["rec-A-copy", "rec-B"]
    assert [source.start_s for source in again] == [
        pytest.approx(noon),
        pytest.approx(noon + _SPLIT_PART_S),
    ]
    assert (
        "[ingest] rec-A-copy.wav keeps declared start 2026-01-01 12:00:00: "
        "declared in the manifest"
    ) in [event.message for event in events]

    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.method == "declared-start"
    assert alignment.offsets["rec-B"] == pytest.approx(_SPLIT_PART_S)


def test_ingest_keeps_a_name_declared_start_across_a_fold(tmp_path) -> None:
    """A folded copy is the same audio, so the start its *name* states is not lost
    with it: the survivor carries it (ticket 218's fold meets ticket 221's name).

    Two 90 s parts of one recorder, 90 s apart, are placed by their names — they
    share no passage, so nothing else can place them. A byte-identical copy of the
    first, named ``REC-copy.wav`` (``-`` sorts before ``_``, so the copy is what
    survives the fold), states no start in its own name at all: without the
    inheritance the only evidence that the pair is sequential leaves with the
    folded name, and the second part is left to whatever two unrelated passages
    correlate at.
    """
    from clear_record.core import JobEvent
    from clear_record.engine import SYNTH_SR, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_SHORT_SPLIT_TAKE_S, 2, seed=7)
    half = scene.size // 2
    for name, part in (
        ("REC_20260101_120000.wav", scene[:half]),
        ("REC_20260101_120130.wav", scene[half:]),
    ):
        sf.write(str(wd / name), part, SYNTH_SR)
    shutil.copyfile(wd / "REC_20260101_120000.wav", wd / "REC-copy.wav")

    events: list[JobEvent] = []
    first, second = stages.ingest(str(wd), on_event=events.append).sources

    # The copy is the survivor; the declaration travels with it, and the line
    # names the file whose name declared it.
    assert [first.id, second.id] == ["REC-copy", "REC_20260101_120130"]
    assert first.start_s is not None and second.start_s is not None
    assert second.start_s - first.start_s == pytest.approx(_SHORT_SPLIT_TAKE_S / 2)
    assert (
        "[ingest] REC_20260101_120000.wav declares start 2026-01-01 12:00:00 "
        "(from its filename): carried into the manifest"
    ) in [event.message for event in events]

    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.method == "declared-start"
    assert alignment.offsets[second.id] == pytest.approx(_SHORT_SPLIT_TAKE_S / 2)


def test_align_places_an_in_band_declared_split_by_its_declaration(
    tmp_path,
) -> None:
    """Two parts of one recorder 90 s apart, inside the estimator's own band: the
    declaration still places them, because the parts **cannot overlap** — they
    are 90 s long and 90 s apart, so no passage is shared, and a correlation run
    over them reports two unrelated passages matching (the reviewer measured
    +21.3330 s, conf 0.505, against a true +90 s). The audio is not consulted."""
    from clear_record.engine import SYNTH_SR, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_SHORT_SPLIT_TAKE_S, 2, seed=7)
    half = scene.size // 2
    for name, part in (
        ("REC_20260101_120000.wav", scene[:half]),
        ("REC_20260101_120130.wav", scene[half:]),
    ):
        sf.write(str(wd / name), part, SYNTH_SR)

    _first, second = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.offsets[second.id] == pytest.approx(_SHORT_SPLIT_TAKE_S / 2)
    assert alignment.method == "declared-start"


def test_align_ignores_a_verdict_over_parts_that_cannot_overlap(
    tmp_path, monkeypatch
) -> None:
    """The rule 221-R1 is about, with the estimator's verdict made explicit: over
    two 90 s parts 90 s apart a correlation reports two unrelated passages
    matching — the reviewer's repro read +21.3330 s, conf 0.505, against a true
    +90 s — and that verdict must not be used, because the parts cannot overlap:
    there is nothing for it to have found. It is therefore not even asked."""
    from clear_record.engine import SYNTH_SR, align as engine_align, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_SHORT_SPLIT_TAKE_S, 2, seed=7)
    half = scene.size // 2
    for name, part in (
        ("REC_20260101_120000.wav", scene[:half]),
        ("REC_20260101_120130.wav", scene[half:]),
    ):
        sf.write(str(wd / name), part, SYNTH_SR)

    asked: list[str] = []

    def spurious(reference_path, source_path, **kwargs):
        asked.append(source_path)
        return 21.333, 0.505

    monkeypatch.setattr(engine_align, "estimate_offset", spurious)
    _first, second = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert asked == []  # not consulted: the two recordings cannot share a passage
    assert alignment.unresolved == ()
    assert alignment.offsets[second.id] == pytest.approx(_SHORT_SPLIT_TAKE_S / 2)
    assert alignment.method == "declared-start"


def test_align_asks_the_audio_for_a_later_take_inside_an_earlier_one(tmp_path) -> None:
    """A shorter take that starts *later* still lies inside a longer earlier one:
    the earlier tape's tail covers the later one's start, so the two share
    passage and the pair is the audio's to place (R7). Bounding the overlap by
    the *shorter* recording instead of by the one that began first called this
    pair sequential and placed it from its names alone — discarding a measured
    +40.0 s at conf 0.983."""
    from clear_record.engine import SYNTH_SR, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_OVERLAP_TAKE_S, 2, seed=7)
    sf.write(str(wd / "TX01_20260101_120000.wav"), scene, SYNTH_SR)
    # 20 s of the same event beginning 40 s in: 20 s of shared passage.
    sf.write(str(wd / "TX01_20260101_120040.wav"), scene[40 * SYNTH_SR :], SYNTH_SR)

    _first, later = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.offsets[later.id] == pytest.approx(40.0, abs=0.5)
    # the tape placed it, and the record says so rather than crediting the name
    assert alignment.method == "windowed-cross-correlation"
    assert alignment.confidence is not None and alignment.confidence > 0.9


def test_align_places_a_declared_pair_that_meets_inside_its_pre_roll(tmp_path) -> None:
    """The allowance for a rotating recorder's pre-roll: its parts meet *inside*
    a correlation window of each other (90 s parts 88 s apart here), so the pair
    is still sequential — and the declaration places it where the estimator,
    seeing only the 2 s the two share, finds nothing at all."""
    from clear_record.engine import SYNTH_SR, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_SHORT_SPLIT_TAKE_S, 2, seed=7)
    sf.write(str(wd / "REC_20260101_120000.wav"), scene[: 90 * SYNTH_SR], SYNTH_SR)
    sf.write(
        str(wd / "REC_20260101_120128.wav"),
        scene[88 * SYNTH_SR : 178 * SYNTH_SR],
        SYNTH_SR,
    )

    _first, second = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.offsets[second.id] == pytest.approx(88.0)
    assert alignment.method == "declared-start"


def test_align_places_a_declared_pair_inside_one_window_of_the_earlier_part(
    tmp_path,
) -> None:
    """The pre-roll allowance itself, pinned where a stub cannot stand in for it.

    The pair here *meets* inside one correlation window of the earlier part's
    length (90 s and 88 s files, declared 88 s apart), so ``_cannot_overlap``
    reads it as a rotation and the declaration places it — but the audio would
    answer for this pair, and answer with the opposite: the two files carry the
    same passage, so ``estimate_offset`` measures the 2 s arming skew their
    whole-second names miss. Remove the allowance (``- _WINDOW_S``) from the
    predicate and that +2 s is what lands in the record. The existing pre-roll
    test cannot show this: its parts share 2 s and stop there, so the estimator
    returns no verdict, and the declaration places the pair either way.
    """
    from clear_record.engine import SYNTH_SR, align as engine_align, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(90.0, 2, seed=7)
    skew = scene.size - 88 * SYNTH_SR  # the 2 s both files really share
    sf.write(str(wd / "REC_20260101_120000.wav"), scene, SYNTH_SR)
    sf.write(str(wd / "REC_20260101_120128.wav"), scene[skew:], SYNTH_SR)

    # the estimator has a verdict over this pair, and it is the audio's own
    off, conf = engine_align.estimate_offset(
        str(wd / "REC_20260101_120000.wav"), str(wd / "REC_20260101_120128.wav")
    )
    assert conf is not None and conf > 0.5
    assert off == pytest.approx(2.0, abs=0.5)

    _first, second = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    # ... and the declaration decides anyway: inside one window of the earlier
    # part's length, the pair is a rotation, and the audio is not asked.
    assert alignment.offsets[second.id] == pytest.approx(88.0)
    assert alignment.method == "declared-start"


@requires_ffmpeg
def test_align_measures_a_declared_pair_libsndfile_cannot_read(
    tmp_path, monkeypatch
) -> None:
    """A declared pair in a container libsndfile refuses (WavPack here; m4a/aac
    read the same way) is still measured, and still read as sequential.

    Ticket 223 put those suffixes in the walk because ``read_audio`` reaches them
    through ffmpeg, and a manifest may name the recording itself rather than
    ingest's staged copy — so the length this rule reads is the length of a file
    whose header libsndfile cannot open. Read from the header alone, that length
    was unknown, which left the pair to the audio; over two parts that cannot
    overlap the estimator answers with two unrelated passages matching (the
    reviewer's repro: +21.3330 s, conf 0.505, against the true +90 s), the
    misplacement ticket 221 is about. The estimator is stubbed to that reading
    here, and must not be asked at all.
    """
    from datetime import datetime, timezone

    from clear_record.core import Source
    from clear_record.engine import align as engine_align
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    for name in ("REC_20260101_120000", "REC_20260101_120130"):
        _write_tone(wd / f"{name}.wav", seconds=90.0)
        subprocess.run(
            [
                _FFMPEG,
                "-v",
                "error",
                "-y",
                "-i",
                str(wd / f"{name}.wav"),
                "-c:a",
                "wavpack",
                str(wd / f"{name}.wv"),
            ],
            check=True,
        )
        (wd / f"{name}.wav").unlink()

    asked: list[str] = []

    def spurious(reference_path, source_path, **kwargs):
        asked.append(source_path)
        return 21.333, 0.505

    monkeypatch.setattr(engine_align, "estimate_offset", spurious)

    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    Workspace.at(wd).write_manifest(
        [
            Source(id="part-1", path=str(wd / "REC_20260101_120000.wv"), start_s=noon),
            Source(
                id="part-2",
                path=str(wd / "REC_20260101_120130.wv"),
                start_s=noon + 90.0,
            ),
        ]
    )
    alignment = stages.align(str(wd))
    assert asked == []  # nothing for the audio to answer: the pair is sequential
    assert alignment.unresolved == ()
    assert alignment.offsets["part-2"] == pytest.approx(90.0)
    assert alignment.method == "declared-start"


def test_align_lets_the_audio_place_a_stamped_pair_that_can_overlap(tmp_path) -> None:
    """The other side of the same rule: two *simultaneous* devices whose names
    disagree with their audio. A whole-second stamp does not resolve an arming
    skew, so where the two recordings can overlap the audio places them — 30 s
    apart by name, 5 s apart on the tape, and the tape wins."""
    from clear_record.engine import SYNTH_SR, make_scene

    wd = tmp_path / "rec"
    wd.mkdir()
    scene, _ = make_scene(_OVERLAP_TAKE_S, 2, seed=7)
    sf.write(str(wd / "TX01_20260101_120000.wav"), scene, SYNTH_SR)
    # The same scene 5 s in: ref_time = source_time + 5, i.e. +5 s.
    sf.write(str(wd / "TX02_20260101_120030.wav"), scene[5 * SYNTH_SR :], SYNTH_SR)

    _room, later = stages.ingest(str(wd)).sources
    alignment = stages.align(str(wd))
    assert alignment.unresolved == ()
    assert alignment.offsets[later.id] == pytest.approx(5.0, abs=0.5)
    # the audio spoke, so the record says so (and carries its confidence)
    assert alignment.method == "windowed-cross-correlation"
    assert alignment.confidence is not None and alignment.confidence > 0.0


def test_ingest_declares_no_start_for_a_name_that_states_no_clock_time(
    tmp_path,
) -> None:
    """A name that states only a date — or only minutes — does not say when a
    part *started*. Reading one as a start would declare two devices that share a
    day simultaneous, the exact confusion a declared start exists to prevent, so
    such a name declares nothing and its source is estimated as before.

    The recorder names the owner's own playbook records — `mac-05`, `tx01`… —
    carry no clock time at all, so they are in here too: the accepted shapes are
    this pass's set, not a vendor's convention.
    """
    from clear_record.core import JobEvent

    wd = tmp_path / "rec"
    wd.mkdir()
    for index, name in enumerate(
        (
            "meeting-2026-01-01.wav",
            "meeting_20260101_1200.wav",
            "tx01.wav",
            "mac-05.wav",
        )
    ):
        _write_tone(wd / name, freq=140.0 + index * 10.0, gain=0.4)

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources
    assert [source.start_s for source in sources] == [None] * 4
    assert not [event for event in events if "declares start" in event.message]


@pytest.mark.parametrize(
    "broken",
    (
        "{}",
        '{"alignment": {"reference": "a"}}',
        "[1, 2]",
        '{"sources": [{"id": "a"}]}',
        '{"sources": {"a": {"id": "a", "path": "a.wav"}}}',
        '{"sources": [{"id": "a", "path": "a.wav", "start_s": "noon"}]}',
        '{"sources": [{"id": "a", "path": "a.wav", "start_s": 1767000000000.0}]}',
        '{"sources": [{"id": "a", "path": "a.wav", "start_s": true}]}',
        '{"sources": [{"id": ["a"], "path": "a.wav", "start_s": 10.0}]}',
    ),
)
def test_ingest_survives_a_manifest_a_hand_edit_broke(tmp_path, broken) -> None:
    """The hand-declare flow is exactly what produces a manifest the pass cannot
    take a declaration out of, and `ingest` must not end in a traceback over one:
    the pass has not been asked to *use* the manifest, only not to lose what it
    says, so a broken one costs the record its declarations and nothing else
    (R8, R9). A `start_s` the record cannot render is one of those shapes however
    numeric it looks — the last four entries are a name, a year-57 970 number, a
    bool, and an id that cannot key anything — and each is dropped rather than
    carried into a source or a sink line."""
    from clear_record.pipeline.workspace import Workspace

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=140.0)
    Workspace.at(wd).manifest_path.write_text(broken, encoding="utf-8")

    sources = stages.ingest(str(wd)).sources
    assert [source.id for source in sources] == ["a"]
    assert sources[0].start_s is None


def test_ingest_reads_a_start_off_a_dotted_name(tmp_path) -> None:
    """The accepted shapes, spelled out in `stages._STAMP_RE`: a dotted date with
    a dotted clock time is one of them (a name outside the set declares nothing,
    and its source is estimated as before)."""
    from datetime import datetime, timezone

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "2026.01.01_12.03.20.wav", freq=180.0)

    (source,) = stages.ingest(str(wd)).sources
    assert source.start_s == pytest.approx(
        datetime(2026, 1, 1, 12, 3, 20, tzinfo=timezone.utc).timestamp()
    )


def test_align_survives_a_hand_declared_start_no_clock_can_render(tmp_path) -> None:
    """The same hand-edit class on the other reader (R10): `align` *subtracts*
    `Source.start_s`, so a value that arrived by hand-edit has to be usable or
    absent there too. A manifest declaring `a: 12:00:00` and `b: "noon"` places
    `b` by its audio — the two chirps of the fixture correlate at zero — where it
    used to raise `TypeError` out of the stage over a field a user typed."""
    import json
    from datetime import datetime, timezone

    from clear_record.pipeline.workspace import Workspace

    wd = _workspace(tmp_path)
    stages.ingest(wd)  # the manifest this test then hand-edits
    w = Workspace.at(wd)
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    manifest = json.loads(w.manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["sources"]:
        entry["start_s"] = noon if entry["id"] == "a" else "noon"
    w.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    alignment = stages.align(wd)
    assert alignment.unresolved == ()
    assert alignment.offsets["b"] == pytest.approx(0.0, abs=0.1)
    assert alignment.method == "windowed-cross-correlation"


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


def _unreadable_declaration(workspace: Path, shape: str) -> Path:
    """Plant a declaration nothing can read, and return it.

    Two shapes, because a file refuses to be read in two ways and the code that
    says so must not care which: ``permission`` (a mode nothing may read) and
    ``not-utf8`` (a declaration saved in the machine's own encoding — GBK,
    cp1252 — which is a plausible field file, not a corrupt one).
    """
    declaration = workspace / ".clear-record-ignore"
    declaration.write_bytes(b"caf\xe9\n" if shape == "not-utf8" else b"*.wav\n")
    if shape == "permission":
        declaration.chmod(0o000)
    return declaration


@pytest.mark.parametrize("shape", ["permission", "not-utf8"])
def test_ingest_refuses_a_declaration_it_cannot_read(tmp_path, shape) -> None:
    """A declaration the pass cannot read is its refusal, in its own words.

    The walk reads the workspace's own ``.clear-record-ignore``, so a file the
    pass cannot read leaves it unable to know which of the folder's files are
    inputs — and taking every one of them would be exactly the silent wrong
    answer the declaration exists to prevent. It says so as the pipeline does:
    one ``[ingest] cannot read …`` line naming the file and the reason, and the
    command surface exits on it — never a traceback through the walk, and never a
    codec's own text as the whole answer.
    """
    from clear_record.cli import cli

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    declaration = _unreadable_declaration(wd, shape)

    with pytest.raises(stages.PipelineError, match="cannot read") as refused:
        stages.ingest(str(wd))
    # The file it could not read, and the reason (the OS's text follows the
    # machine's locale, so only the shape is pinned here).
    assert str(refused.value).startswith(f"[ingest] cannot read {declaration}: ")
    assert str(refused.value) != f"[ingest] cannot read {declaration}: "

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ingest", str(wd)])
    assert str(exit_info.value.code) == str(refused.value)


@pytest.mark.parametrize("shape", ["permission", "not-utf8"])
def test_the_commands_own_auto_probe_refuses_an_unreadable_declaration(
    tmp_path, shape
) -> None:
    """``--auto``'s probe walks the folder in this process, and it says the same.

    The probe measures the tapes to choose a profile, so it reads the folder's
    own declaration before anything is decoded. A declaration the command cannot
    read is the declaration's own refusal — the same id the run edges answer —
    rendered where a person reads it, not a traceback out of the walk.
    """
    from clear_record.cli import cli
    from clear_record.pipeline.workspace import CANNOT_READ_DECLARATION

    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _unreadable_declaration(wd, shape)

    with pytest.raises(SystemExit) as ended:
        cli.main(["transcribe", str(wd), "--auto"])

    assert str(ended.value) == CANNOT_READ_DECLARATION


def test_ingest_applies_a_declaration_saved_with_a_byte_order_mark(tmp_path) -> None:
    """A declaration saved "UTF-8 with BOM" still names what it names.

    An editor that writes the mark puts ``\\ufeff`` in front of the first line, so
    a declaration read as plain utf-8 yields ``"\\ufeffb.wav"`` for its pattern,
    matches nothing, and the file the operator declared out is decoded with **no
    refusal and no line** — the silent non-application this read exists to
    prevent. The mark is stripped (a no-op when it is absent), so the one-line
    declaration the README documents keeps applying; the no-mark, comment and
    blank-line shapes are covered by the tests either side of this one.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=330.0)
    (wd / ".clear-record-ignore").write_bytes(b"\xef\xbb\xbfb.wav\n")

    from clear_record.core import JobEvent
    from clear_record.pipeline.workspace import discover_audio

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources

    assert [s.id for s in sources] == ["a"]
    assert [p.name for p in discover_audio(wd)] == ["a.wav"]
    assert [e.message for e in events if "excluded" in (e.message or "")] == [
        "[ingest] excluded b.wav: named in .clear-record-ignore"
    ]
