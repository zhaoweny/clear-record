"""The commands' default stdout, pinned byte for byte.

``cli/cli.py`` states the contract — the stages print nothing and the command
surface prints every word they report, so a stage command's default stdout is
byte-identical — and nothing enforced it: the suite's only exact-stdout assertion
was an *empty* output (``test_diagnostics_cli``), and every other stage assertion
is a substring check. This module is the pin: the eight stage-bearing commands,
the ``run`` and ``calibrate`` aggregates a user actually types, and the ``synth``
development command. ``run`` is the one block whose stage lines this surface does
not print itself: a command-line run is the node's (ADR-0032), so the follower
reads them back off the run's own stream (``cli/runs.py``) — the same text the
stage commands print, because the stages report it as events on that one channel
and the follower prints every message it carries. That block's other two lines
are the surface's own — ``[run] #<id> <status>`` and the ``[next]`` pointer
(``_cmd_run``) — where every other block's lines are all its own.

What makes it a stable pin rather than a flaky one:

- ``CR_JOBS=1``: the chunk pool is then serial, so the lines the transcriber
  reports keep one order — the pool's interleaving is what would otherwise
  move. (The stages print nothing at all: a line is reported on the stage's
  sink, which the command surface renders to stdout and which ``Workspace.log``
  keeps in ``transcribe.log``.)
- a **cold** chunk cache (``CR_CACHE_DIR`` under the test's ``tmp_path``, which
  does not exist yet), and one fresh workspace per block, so no command's
  output depends on what an earlier one left behind — the pending/cached/
  carried lines differ on a warm cache, and so do the reconcile and export
  lines of a second pass;
- ``CR_LANG=en``, and every other ``CR_*`` variable cleared, so the machine's
  own environment cannot reach a knob or swap the catalog;
- the ``run`` block carries no ``[queued]`` line, and does not here: the follower
  prints a run's queue place only from a **poll** (``cli.runs.follow`` — the
  read after the one it starts with), and this node has one run and nothing
  gating it, so the run is claimed within milliseconds while that poll is a
  second away. A run that is *still* queued a full poll after the node accepted
  it is the only thing that adds a line there;
- the fixture directory is normalised out of the captured text, because every
  printed path is absolute;
- nothing machine- or clock-derived is printed at all: the fake backend sizes
  no work by CPU count or VRAM, and the one artifact that embeds ``now()``
  (``export/record.json``) is written, never read back.

It is a **text-and-order pin, not a timing pin**: the same bytes in the same
order, not at the same moment.

The capture is also the equivalence check the pipeline's own extraction uses:
the commands are driven through the real Click group, so a stage that stops
printing and a command surface that starts rendering it must produce these
bytes between them.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from clear_record.cli import cli
from clear_record.core import JobEvent, Segment, TranscriptionResult
from clear_record.core.process import ProcessRunner
from clear_record.pipeline import stages
from clear_record.providers import BackendBase, BackendInfo

#: The whole capture, in the order it was taken. Regenerate by running
#: :func:`_capture` once and pasting its text here; a mismatch is a byte-level
#: diff. Every block but ``run`` is the **pre-move** one — captured against
#: 1277852 — so those double as the move's equivalence check; ``run``'s stage
#: lines are those same bytes, read back from the node the run executes on (the
#: stage commands' own text, printed by the follower from the run's stream),
#: with the surface's end line before ``[next]``.
_GOLDEN = """\
$ clear-record glossary --add Clear Record --add Oh My Pi
[glossary] $FIXTURE/tape/glossary.txt (2 term(s))
  Clear Record
  Oh My Pi
$ clear-record ingest
[ingest] decode a.wav -> a.wav
[ingest] decode b.wav -> b.wav
[ingest] decode c.wav -> c.wav
[ingest] 3 source(s) -> $FIXTURE/tape/manifest.json
  a                        $FIXTURE/tape/audio/a.wav
  b                        $FIXTURE/tape/audio/b.wav
  c                        $FIXTURE/tape/audio/c.wav
$ clear-record align
[align] reference=a method=windowed-cross-correlation conf=1.0 unresolved=1
  a                        offset=+0.0000s (ref)
  b                        offset=+0.0000s
  c                        UNRESOLVED (could not place this source)
$ clear-record transcribe
[transcribe] glossary: 22 chars from $FIXTURE/tape/glossary.txt
[transcribe] a: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] b: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] c: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] 3 pending chunk(s), jobs=1 (fake-model)
[transcribe]   a chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   b chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   c chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe] chunks: 3 re-decoded, 0 reused
[transcribe] 'fake-model' via apple -> segments.json
  a                        segments=   1  duration=6.0  chunks=1
  b                        segments=   1  duration=6.0  chunks=1
  c                        segments=   1  duration=6.0  chunks=1
$ clear-record diarize
[diarize] a: 1 speaker(s) over 1 segment(s)
[diarize] b: 1 speaker(s) over 1 segment(s)
[diarize] c: 1 speaker(s) over 1 segment(s)
$ clear-record attribute
[attribute] 3 segment(s), 1 speaker(s), 2 re-attributed
$ clear-record glossary
[glossary] $FIXTURE/tape/glossary.txt (2 term(s))
  Clear Record
  Oh My Pi
$ clear-record reconcile
[reconcile] 1 segment(s), 1 attributed speaker(s) -> $FIXTURE/tape/record.json
  00:00:00.000 [Speaker 3] chunk
$ clear-record export
[export] md   -> $FIXTURE/tape/export/record.md
[export] srt  -> $FIXTURE/tape/export/record.srt
[export] vtt  -> $FIXTURE/tape/export/record.vtt
[export] json -> $FIXTURE/tape/export/record.json
$ clear-record run
[ingest] decode a.wav -> a.wav
[ingest] decode b.wav -> b.wav
[ingest] decode c.wav -> c.wav
[ingest] 3 source(s) -> $FIXTURE/run/manifest.json
  a                        $FIXTURE/run/audio/a.wav
  b                        $FIXTURE/run/audio/b.wav
  c                        $FIXTURE/run/audio/c.wav
[align] reference=a method=windowed-cross-correlation conf=1.0 unresolved=1
  a                        offset=+0.0000s (ref)
  b                        offset=+0.0000s
  c                        UNRESOLVED (could not place this source)
[transcribe] a: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] b: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] c: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] 3 pending chunk(s), jobs=1 (fake-model)
[transcribe]   a chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   b chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   c chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe] chunks: 3 re-decoded, 0 reused
[transcribe] 'fake-model' via apple -> segments.json
  a                        segments=   1  duration=6.0  chunks=1
  b                        segments=   1  duration=6.0  chunks=1
  c                        segments=   1  duration=6.0  chunks=1
[reconcile] 1 segment(s), 1 attributed speaker(s) -> $FIXTURE/run/record.json
  00:00:00.000 [Speaker 1] chunk
[export] md   -> $FIXTURE/run/export/record.md
[export] srt  -> $FIXTURE/run/export/record.srt
[export] vtt  -> $FIXTURE/run/export/record.vtt
[export] json -> $FIXTURE/run/export/record.json
[run] #1 done
[next] the record is in $FIXTURE/run/export; review it and accept the minutes in the console: `clear-record web`
$ clear-record calibrate
[ingest] decode a.wav -> a.wav
[ingest] decode b.wav -> b.wav
[ingest] decode c.wav -> c.wav
[ingest] 3 source(s) -> $FIXTURE/calibrate/manifest.json
  a                        $FIXTURE/calibrate/audio/a.wav
  b                        $FIXTURE/calibrate/audio/b.wav
  c                        $FIXTURE/calibrate/audio/c.wav
[align] reference=a method=windowed-cross-correlation conf=1.0 unresolved=1
  a                        offset=+0.0000s (ref)
  b                        offset=+0.0000s
  c                        UNRESOLVED (could not place this source)
[transcribe] a: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] b: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] c: 1 chunk(s), 6.0s, fake decoder (fake-model)
[transcribe] 3 pending chunk(s), jobs=1 (fake-model)
[transcribe]   a chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   b chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe]   c chunk 1/1 [0-6s] -> 1 segment(s)
[transcribe] chunks: 3 re-decoded, 0 reused
[transcribe] 'fake-model' via apple -> segments.json
  a                        segments=   1  duration=6.0  chunks=1
  b                        segments=   1  duration=6.0  chunks=1
  c                        segments=   1  duration=6.0  chunks=1
[reconcile] 1 segment(s), 1 attributed speaker(s) -> $FIXTURE/calibrate/record.json
  00:00:00.000 [Speaker 1] chunk
[export] md   -> $FIXTURE/calibrate/export/record.md
[export] srt  -> $FIXTURE/calibrate/export/record.srt
[export] vtt  -> $FIXTURE/calibrate/export/record.vtt
[export] json -> $FIXTURE/calibrate/export/record.json

[calibrate] report:
  source_duration    6.0
  transcript_span    6.0
  coverage           1.0
  segments           1
  mean_confidence    None
  words              1
  -> $FIXTURE/calibrate/export/calibration.json
$ clear-record synth
[synth] 4 device(s), 34 speaker event(s) -> $FIXTURE/synth
  device_0   true_offset=+0.0000s
  device_1   true_offset=+1.1410s
  device_2   true_offset=+1.1070s
  device_3   true_offset=+0.5720s
  ground truth -> $FIXTURE/synth/ground_truth.json
"""

#: The stage-bearing commands, in the order one workspace meets them. The
#: glossary is filled first so the transcribe stage's prompt line is part of
#: the pin; `diarize`, `attribute` and `glossary` are driven because they print
#: through the same two channels as the five declared stages.
_STAGE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("glossary", "--add", "Clear Record", "--add", "Oh My Pi"),
    ("ingest",),
    ("align",),
    ("transcribe",),
    ("diarize",),
    ("attribute",),
    ("glossary",),
    ("reconcile",),
    ("export",),
)

#: The remaining blocks, each on its own cold workspace (``(workspace name,
#: commands)``): `run` is every stage plus the command surface's two lines —
#: ``[run] #<id> <status>`` and ``[next]``, the latter the one whose ``tr`` is
#: why ``CR_LANG`` is pinned. `calibrate` is that run plus the report it writes
#: and prints, and `synth` is the development command that builds the fixture a
#: `calibrate` run is scored on. `run` and `calibrate` are what a user actually
#: types and the aggregates the stages' return change rewires; `calibrate`'s
#: report and `synth` are the two helpers the pipeline package hands back to the
#: command surface.
_BLOCKS: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("tape", _STAGE_COMMANDS),
    ("run", (("run",),)),
    ("calibrate", (("calibrate",),)),
    ("synth", (("synth",),)),
)

#: The fixture's audio: 6 s, 8 kHz. ``a`` and ``b`` are one *chirp* — a
#: stationary tone would leave the correlation peak ambiguous every period —
#: and ``b`` is the same chirp with its **opening second silenced**, so
#: alignment finds one unambiguous lag at zero and the printed numbers are
#: exact: ``conf=1.0`` and ``offset=+0.0000s``. A *shifted* copy would print a
#: computed coefficient instead (``0.9818916874062776`` measured), whose last
#: digits belong to the FFT implementation — exactly the flakiness this pin
#: must not have. ``c`` is seeded noise over the same 6 s at 8 kHz, sharing
#: nothing with either, so the UNRESOLVED line is part of the pin.
_CHIRP_S = 6.0
_CHIRP_SR = 8000
_CHIRP_F0 = 100.0
_CHIRP_F1 = 400.0
_CHIRP_SILENCE_S = 1.0
_NOISE_SEED = 20260921


class _FakeBackend(BackendBase):
    """A deterministic decoder: one segment spanning each chunk it is handed."""

    info = BackendInfo(
        id="fake",
        vendor="test",
        frameworks=(),
        description="fake decoder",
        default_model="fake-model",
    )

    def available(self) -> bool:
        return True

    def prepare(self, model: str | None, model_dir: str | None) -> str:
        return "fake-model.bin"

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
        process_runner: ProcessRunner | None = None,
        **decoder_knobs: object,
    ) -> TranscriptionResult:
        data, sample_rate = sf.read(audio_path)
        duration = len(data) / sample_rate
        return TranscriptionResult(
            source="fake",
            segments=(Segment(0.0, round(duration, 3), "chunk", "fake"),),
            language="en",
            backend="fake",
            model="fake-model",
            audio_duration=duration,
        )


def _seconds() -> np.ndarray:
    return np.arange(int(_CHIRP_S * _CHIRP_SR), dtype=np.float64) / _CHIRP_SR


def _write_chirp(path: Path, *, silence_s: float = 0.0) -> None:
    seconds = _seconds()
    sweep = (_CHIRP_F1 - _CHIRP_F0) / (2.0 * _CHIRP_S)
    phase = 2 * np.pi * (_CHIRP_F0 * seconds + sweep * seconds * seconds)
    wave = 0.4 * np.sin(phase)
    wave[: int(silence_s * _CHIRP_SR)] = 0.0
    sf.write(str(path), wave.astype(np.float32), _CHIRP_SR)


def _write_noise(path: Path) -> None:
    generator = np.random.default_rng(_NOISE_SEED)
    wave = 0.2 * generator.standard_normal(_seconds().size)
    sf.write(str(path), wave.astype(np.float32), _CHIRP_SR)


def _fixture(workspace: Path) -> Path:
    workspace.mkdir()
    _write_chirp(workspace / "a.wav")
    _write_chirp(workspace / "b.wav", silence_s=_CHIRP_SILENCE_S)
    _write_noise(workspace / "c.wav")
    return workspace


def _capture(root: Path, bring_up) -> str:
    """Run the stage commands over a fresh workspace under ``root``.

    Returns their stdout with the fixture root replaced by ``$FIXTURE``.

    A node is up for the whole capture: ``run``'s stages execute on it (ADR-0032,
    a command-line run is the node's run), and this is the only block that needs
    one. It is brought up the way a posture brings one up — the app on an
    ephemeral port, its address recorded — and it is the same app object, so the
    block's bytes are the surface's, not a stub's.
    """
    monkey = pytest.MonkeyPatch()
    try:
        for name in list(os.environ):
            if name.startswith("CR_"):
                monkey.delenv(name)
        monkey.setenv("CR_JOBS", "1")
        monkey.setenv("CR_LANG", "en")
        monkey.setenv("CR_CACHE_DIR", str(root / "cache"))
        monkey.setenv("CR_DATA_DIR", str(root / "data"))
        monkey.setenv("CR_STATE_DIR", str(root / "state"))
        monkey.setenv("CR_LOG_DIR", str(root / "logs"))
        # ``--backend`` is a Click choice over the provider catalog; the
        # commands are invoked without it, so the catalog's own default id
        # applies, and the fake is substituted at the seam the stages call. The
        # id they print is therefore the catalog's default, which keeps that
        # line tied to the declaration rather than to a literal in this file.
        monkey.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
        # `run` ensures a node before it submits its run (ADR-0032), and the node
        # the fixture brought up is the one it attaches to: the address is
        # recorded, so `ensure_node()`'s attach path resolves it instead of
        # starting a second node. Nothing about the node is stubbed here — the
        # `run` block's bytes are the real surface's, over the same app object.

        # Cold: a warm chunk cache prints cached/kept lines instead, and the pin
        # would then describe the cache's state rather than the stages' output.
        assert not (root / "cache").exists()

        runner = CliRunner()
        group = cli._build_group()
        captured = ""
        with bring_up(root=root):
            for name, commands in _BLOCKS:
                workspace = _fixture(root / name)
                for command in commands:
                    result = runner.invoke(group, [*command, str(workspace)])
                    assert result.exit_code == 0, (
                        command,
                        result.output,
                        result.exception,
                    )
                    captured += f"$ clear-record {' '.join(command)}\n{result.output}"
        for form in (str(root), str(root.resolve())):
            captured = captured.replace(form, "$FIXTURE")
        return captured
    finally:
        monkey.undo()


def test_commands_print_the_pinned_bytes(tmp_path: Path, node_in_this_process) -> None:
    """Every command's default stdout, byte for byte, over one capture."""
    assert _capture(tmp_path, node_in_this_process) == _GOLDEN


def test_the_surface_prints_only_the_words_the_channel_carries(capsys) -> None:
    """Nothing is printed that no event carries.

    The command surface's whole rendering is the sink: a progress report — the
    counters the console's bar and rate read — prints nothing at all, and an
    event that carries a line prints its ``message``. So every byte a stage
    command writes is a byte the run's stream carries for every other client.
    """
    sink = cli._StageLines()
    sink(JobEvent(stage="transcribe", index=1, total=2, elapsed_s=1.0, source="a"))
    sink(JobEvent(stage="transcribe", index=2, total=2, elapsed_s=2.0, done=True))

    assert capsys.readouterr().out == ""

    sink(JobEvent(stage="transcribe", index=2, total=2, message="[transcribe] done"))
    assert capsys.readouterr().out == "[transcribe] done\n"
