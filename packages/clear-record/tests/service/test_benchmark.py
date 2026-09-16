"""BENCH-01: the four axes, computed once and honest about what is unknown.

Every test seeds a run or a workspace directly -- no test depends on wall-clock
durations, a real decoder or a GPU. One test drives the real pipeline with a
fake backend (and the real :class:`RunManager` cost record) to prove the axes
read back what a run recorded.
"""

from __future__ import annotations

import sys
import threading
import time
from importlib.metadata import entry_points

import click
import numpy as np
import pytest
import soundfile as sf
from click.testing import CliRunner

from clear_record.cli import cli as cli_module
from clear_record.cli import stages
from clear_record.cli.transcription import WorkerRssSampler
from clear_record.cli.workspace import Workspace
from clear_record.core import RecordDocument, Segment, Source, TranscriptionResult
from clear_record.providers import BackendBase, BackendInfo, CancellableProcessRunner
from clear_record.service import (
    PipelineOptions,
    Registry,
    RunManager,
    benchmark,
    run_axes,
)

#: The exact shape each axis promises. A test that adds a field here is also
#: pinning that the console and the CLI can rely on it.
ACCURACY_KEYS = {
    "basis",
    "wer",
    "similarity",
    "coverage",
    "mean_confidence",
    "words",
    "segments",
    "reason",
}
SPEED_KEYS = {"x_realtime", "audio_seconds", "wall_seconds", "reason"}
MEMORY_KEYS = {"peak_rss_bytes", "reason"}
FIT_KEYS = {"chose", "explanations", "facts", "reason"}

COST = {
    "audio_seconds": 3600.0,
    "total_wall_seconds": 1200.0,
    "jobs": 4,
    "chunk_seconds": 30.0,
    "machine": "Mac14,1",
}

AUTO_META = {
    "profile": "accurate",
    "decoder_knobs": {"beam_size": 8},
    "auto": {
        "explanation": "--auto: chose profile (a short tape)",
        "chose": ["profile", "model"],
    },
}


def _workspace(tmp_path, *, memory: dict | None = None) -> Workspace:
    """A tiny workspace with a manifest, a transcript and a record.

    Ten seconds of source, two segments spanning 0.0-9.0 at confidence 0.9 and
    0.7: coverage 0.9 over two words each, mean confidence 0.8.
    """
    directory = tmp_path / "ws"
    directory.mkdir()
    workspace = Workspace.at(directory)
    source = Source(id="a", path=str(directory / "a.wav"), label="mic")
    workspace.write_manifest([source])
    segments = [
        Segment(start=0.0, end=4.0, text="hello there", source="a", confidence=0.9),
        Segment(start=5.0, end=9.0, text="general kenobi", source="a", confidence=0.7),
    ]
    workspace.write_segments(
        {"a": segments},
        {"sources": {"a": {"duration": 10.0}}, **(memory or {})},
    )
    workspace.write_record(
        RecordDocument(sources=(source,), alignment=None, segments=tuple(segments))
    )
    return workspace


def _seed(
    tmp_path, *, registry: Registry | None = None, memory=None, cost=None, meta=None
):
    """A project, a meeting over :func:`_workspace`, and one run row.

    With ``cost`` the run is ``done`` and carries the RUN-01 record; without it
    the run stays ``queued`` with no cost record (the old-run case).
    """
    registry = registry or Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    workspace = _workspace(tmp_path, memory=memory)
    meeting = registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace.root)
    )
    run = registry.create_run(
        meeting.id,
        backend="apple",
        model="small",
        language="en",
        options=meta or {},
    )
    if cost is not None:
        registry.update_run(run.id, status="done", progress={"cost": cost})
    return registry, registry.get_run(run.id), workspace


def test_the_four_axes_are_present_and_correctly_shaped(tmp_path) -> None:
    registry, run, workspace = _seed(
        tmp_path,
        memory={"peak_rss_bytes": 512 * 1024 * 1024},
        cost=dict(COST),
        meta=dict(AUTO_META),
    )

    axes = run_axes(run, directory=str(workspace.root))

    assert sorted(axes) == ["accuracy", "fit", "memory", "speed"]
    assert set(axes["accuracy"]) == ACCURACY_KEYS
    assert set(axes["speed"]) == SPEED_KEYS
    assert set(axes["memory"]) == MEMORY_KEYS
    assert set(axes["fit"]) == FIT_KEYS

    # accuracy: the coverage/confidence basis when no reference is configured.
    assert axes["accuracy"]["basis"] == "coverage"
    assert axes["accuracy"]["coverage"] == 0.9
    assert axes["accuracy"]["mean_confidence"] == 0.8
    assert axes["accuracy"]["words"] == 4
    assert axes["accuracy"]["segments"] == 2
    assert axes["accuracy"]["wer"] is None
    assert axes["accuracy"]["reason"] is None

    # speed: audio seconds per wall second, derived here and never stored.
    assert axes["speed"]["x_realtime"] == 3.0
    assert axes["speed"]["audio_seconds"] == 3600.0
    assert axes["speed"]["wall_seconds"] == 1200.0
    assert axes["speed"]["reason"] is None

    # memory: the transcribe stage's own measurement, in bytes.
    assert axes["memory"] == {"peak_rss_bytes": 512 * 1024 * 1024, "reason": None}

    # fit: what --auto chose, and the resolved facts it worked with.
    assert axes["fit"]["chose"] == ["model", "profile"]
    assert axes["fit"]["explanations"] == ["--auto: chose profile (a short tape)"]
    assert axes["fit"]["facts"] == {
        "backend": "apple",
        "model": "small",
        "language": "en",
        "profile": "accurate",
        "knobs": {"beam_size": 8},
        "jobs": 4,
        "chunk_seconds": 30.0,
        "machine": "Mac14,1",
    }
    assert axes["fit"]["reason"] is None


def test_accuracy_is_wer_when_a_reference_is_configured(tmp_path) -> None:
    _registry, run, workspace = _seed(tmp_path, cost=dict(COST))
    reference = tmp_path / "ref.txt"
    reference.write_text("hello there", encoding="utf-8")

    axes = run_axes(run, directory=str(workspace.root), reference=reference)

    assert axes["accuracy"]["basis"] == "wer"
    assert isinstance(axes["accuracy"]["wer"], float)
    assert axes["accuracy"]["wer"] > 0
    # The raw coverage numbers stay visible: the axis is not one synthetic score.
    assert axes["accuracy"]["coverage"] == 0.9
    assert axes["accuracy"]["reason"] is None


def test_an_unmeasurable_memory_axis_is_none_with_a_reason_not_zero(tmp_path) -> None:
    # A recorded zero is not a measurement: the axis must stay unknown.
    _registry, run, workspace = _seed(tmp_path, memory={"peak_rss_bytes": 0})
    axes = run_axes(run, directory=str(workspace.root))
    assert axes["memory"]["peak_rss_bytes"] is None
    assert axes["memory"]["peak_rss_bytes"] != 0
    assert axes["memory"]["reason"] == benchmark.NO_MEMORY


def test_the_stages_own_memory_reason_is_passed_through(tmp_path) -> None:
    reason = "no decoder worker ran: every chunk was reused from the cache"
    _registry, run, workspace = _seed(
        tmp_path, memory={"peak_rss_bytes": None, "peak_rss_reason": reason}
    )
    axes = run_axes(run, directory=str(workspace.root))
    assert axes["memory"] == {"peak_rss_bytes": None, "reason": reason}


def test_a_run_with_no_cost_record_yields_unknown_axes_and_never_raises(
    tmp_path,
) -> None:
    _registry, run, workspace = _seed(tmp_path)  # queued: no progress, no cost

    axes = run_axes(run, directory=str(workspace.root))

    assert axes["speed"] == {
        "x_realtime": None,
        "audio_seconds": None,
        "wall_seconds": None,
        "reason": benchmark.NO_COST_RECORD,
    }
    assert axes["memory"]["peak_rss_bytes"] is None
    assert axes["memory"]["reason"] == benchmark.NO_MEMORY

    # A bare call, a missing workspace and an unreadable record are all reasons.
    assert set(run_axes()) == {"accuracy", "speed", "memory", "fit"}
    assert run_axes()["accuracy"]["reason"] == benchmark.NO_WORKSPACE
    assert run_axes()["fit"]["reason"] == benchmark.NO_RUN
    assert (
        run_axes(None, directory=tmp_path / "missing")["accuracy"]["reason"]
        == benchmark.NO_RECORD
    )
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "record.json").write_text("{not json", encoding="utf-8")
    assert run_axes(None, directory=broken)["accuracy"]["reason"] == benchmark.NO_RECORD


class _FakeBackend(BackendBase):
    """An in-process backend: it runs no worker process at all."""

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
        data, file_sr = sf.read(audio_path)
        duration = len(data) / file_sr
        return TranscriptionResult(
            source="fake",
            segments=(
                Segment(
                    start=0.0,
                    end=round(duration, 3),
                    text="hello there",
                    source="fake",
                    confidence=0.5,
                ),
            ),
            language="en",
            backend="fake",
            model="fake",
            audio_duration=duration,
        )


def test_a_fake_backend_run_yields_every_axis(tmp_path, monkeypatch) -> None:
    """The pipeline path: real stages, the real cost record, no decoder.

    The fake backend decodes in-process, so it spawns no worker process -- and
    the memory axis must say so with a reason rather than report a zero.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    directory = tmp_path / "rec"
    directory.mkdir()
    sr = 8000
    tone = np.arange(6 * sr, dtype=np.float64) / sr
    wav = directory / "a.wav"
    sf.write(str(wav), (0.4 * np.sin(2 * np.pi * 220.0 * tone)).astype(np.float32), sr)
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(directory))
    registry.set_recording_set(meeting.id, [str(wav)])
    monkeypatch.setattr(stages, "get_backend", lambda _id: _FakeBackend())

    manager = RunManager(registry)
    try:
        run = manager.start(meeting, PipelineOptions(backend="fake", jobs=1))
        state = manager.wait(run.id, timeout=120)
    finally:
        manager.shutdown(timeout=5)
    assert state.status == "done", state.error

    row = registry.get_run(run.id)
    assert row is not None and row.progress is not None
    axes = run_axes(row, directory=str(directory))

    assert set(axes) == {"accuracy", "speed", "memory", "fit"}
    assert axes["accuracy"]["basis"] == "coverage"
    assert axes["accuracy"]["coverage"] is not None
    assert axes["accuracy"]["words"] is not None
    assert axes["accuracy"]["words"] >= 1
    assert axes["speed"]["audio_seconds"] == pytest.approx(6.0, abs=0.05)
    assert axes["speed"]["x_realtime"] is None or axes["speed"]["x_realtime"] > 0
    assert axes["memory"]["peak_rss_bytes"] is None
    assert axes["memory"]["reason"]
    assert axes["memory"]["reason"] != 0
    assert axes["fit"]["reason"] is None
    assert axes["fit"]["facts"]["backend"] == "fake"

    # The transcribe stage recorded the same unknown-with-reason pair the axis
    # reads -- the CLI-side measurement is what the service renders.
    _per_source, meta = Workspace.at(directory).load_segments()
    assert meta["peak_rss_bytes"] is None
    assert meta["peak_rss_reason"] == axes["memory"]["reason"]


def test_the_worker_sampler_measures_a_live_process(tmp_path) -> None:
    """The memory axis is a measurement: a real child of the pool's runner is
    read while it lives. The wait is bounded and only waits for the child to
    appear -- the assertion never depends on how long it runs."""
    runner = CancellableProcessRunner()
    sampler = WorkerRssSampler(runner, interval_s=0.05)
    sampler.start()
    try:
        worker = threading.Thread(
            target=lambda: runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            ),
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 15
        while sampler.peak_bytes is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sampler.samples > 0
        assert sampler.peak_bytes is not None
        assert sampler.peak_bytes > 0
    finally:
        runner.terminate_all()
        sampler.stop()


def test_the_bench_command_renders_the_service_values(tmp_path) -> None:
    """The CLI prints exactly what the service computed -- line for line."""
    data_dir = tmp_path / "data"
    registry = Registry.open(data_dir=data_dir)
    _registry, run, workspace = _seed(
        tmp_path,
        registry=registry,
        memory={"peak_rss_bytes": 64 * 1024 * 1024},
        cost=dict(COST),
        meta=dict(AUTO_META),
    )
    axes = run_axes(registry.get_run(run.id), directory=str(workspace.root))

    group = click.Group("clear-record")
    benchmark.register(group)
    result = CliRunner().invoke(
        group, ["bench", "--run-id", str(run.id), "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == benchmark.render_axes(axes)
    assert "3.00x realtime" in result.output
    assert "--auto chose model, profile" in result.output


def test_the_bench_command_reports_a_workspace_without_a_run(tmp_path) -> None:
    _registry, _run, workspace = _seed(tmp_path)
    group = click.Group("clear-record")
    benchmark.register(group)

    result = CliRunner().invoke(group, ["bench", "--directory", str(workspace.root)])

    assert result.exit_code == 0, result.output
    assert "accuracy: coverage 0.9000" in result.output
    # No run record: speed and fit say so instead of inventing a number.
    assert "speed: unknown" in result.output
    assert "fit: unknown" in result.output

    missing = CliRunner().invoke(
        group,
        [
            "bench",
            "--directory",
            str(workspace.root),
            "--reference",
            str(tmp_path / "x"),
        ],
    )
    assert missing.exit_code == 2


def test_the_bench_command_is_declared_as_an_entry_point() -> None:
    declared = {
        point.name: point.value
        for point in entry_points(group=cli_module.COMMAND_ENTRY_POINT_GROUP)
    }
    assert declared.get("bench") == "clear_record.service.benchmark:register"
