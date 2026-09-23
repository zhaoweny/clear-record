"""Stages report structured progress, and the CLI's text output is unchanged.

The stages run for real against synthetic tones (no ASR backend needed), so the
event sequence is observed end to end rather than mocked.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from clear_record.pipeline import stages
from clear_record.pipeline.workspace import Workspace
from clear_record.core import JobEvent, RunCancelled, Segment, Step, log_path


def _write_tone(
    path, sr: int = 8000, seconds: float = 6.0, freq: float = 220.0
) -> None:
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sf.write(str(path), (0.4 * np.sin(phase)).astype(np.float32), sr)


def test_stages_emit_progress_and_keep_text_output(tmp_path, capsys) -> None:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _write_tone(wd / "b.wav")

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append)
    stages.align(str(wd), on_event=events.append)

    # `transcribe` is backend-gated, so seed segments by hand (as the CLI tests do).
    Workspace.at(wd).write_segments(
        {
            source.id: [Segment(start=0.0, end=1.0, text="hello", source=source.id)]
            for source in sources
        },
        {"backend": "none", "model": "none"},
    )
    stages.reconcile(str(wd), on_event=events.append)
    stages.export(str(wd), on_event=events.append)

    by_stage: dict[str, list[JobEvent]] = {}
    for event in events:
        by_stage.setdefault(event.stage, []).append(event)

    assert set(by_stage) == {"ingest", "align", "reconcile", "export"}
    for stage, stage_events in by_stage.items():
        assert stage_events[-1].done, stage
        assert stage_events[-1].index == stage_events[-1].total, stage
        indices = [event.index for event in stage_events]
        assert indices == sorted(indices), stage

    assert by_stage["ingest"][-1].total == 2
    assert by_stage["export"][-1].total == 4

    # Attaching a sink must not change the command-line output.
    out = capsys.readouterr().out
    assert "[ingest]" in out
    assert "[align]" in out
    assert "[export]" in out


def test_run_threads_the_sink_to_every_stage(tmp_path, monkeypatch) -> None:
    received = []

    def fake_runner(directory, options, on_event, cancel=None) -> None:
        received.append(on_event)

    monkeypatch.setattr(stages, "_STAGE_RUNNERS", {step: fake_runner for step in Step})
    sink = lambda event: None  # noqa: E731 - trivial sink

    stages.run(str(tmp_path), on_event=sink)

    assert len(received) == len(list(Step))
    assert all(handler is sink for handler in received)


def test_a_sink_that_stops_the_run_stops_the_pipeline(tmp_path, monkeypatch) -> None:
    """A sink raising ``RunCancelled`` unwinds the pipeline where it was called.

    This is how a run's cancel reaches the work (RUN-04): every stage announces
    itself through the sink, and the run queue's sink raises there when the run's
    signal is set. The stage announcing itself is the last thing that runs — the
    stages after it are never entered — and the exception reaches the caller
    rather than being recorded as a failure.
    """
    ran: list[str] = []
    events: list[JobEvent] = []

    def fake_runner(directory, options, on_event, cancel=None) -> None:
        ran.append("ran")
        on_event(JobEvent(stage="ingest", index=0, total=1))

    monkeypatch.setattr(stages, "_STAGE_RUNNERS", {step: fake_runner for step in Step})

    def stopping_sink(event: JobEvent) -> None:
        events.append(event)
        raise RunCancelled("the run was cancelled")

    with pytest.raises(RunCancelled):
        stages.run(str(tmp_path), on_event=stopping_sink)

    assert ran == ["ran"], "the announcing stage ran; the rest never started"
    assert events and events[0].stage == "ingest"

    # A cancellation is an outcome, not a failed pipeline: the log says the run
    # stopped, and nothing reports the pipeline as having failed.
    log = log_path().read_text(encoding="utf-8")
    assert "cli.run.stopped" in log
    assert "cli.run.failed" not in log
