"""RUN-04: cancel a queued or running run, and resume one that stopped early.

The queue-level half of the item, over the real service: a run is cancelled
before it starts (a terminal transition, so nothing ever executes it), a run is
cancelled while it runs (the run queue hands its pipeline a channel whose signal
stops it at the next thing it reports), a *second writer* can ask a run executing
elsewhere to stop, and a resumed run continues from the chunks its predecessor
left cached.

Two of these drive the **real** transcribe stage over a real workspace with a
counting backend, because the acceptance is about what the run's own cost record
says: a cache that is actually re-used, not a number a fake wrote.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli import stages
from clear_record.core import (
    Progress,
    Segment,
    TranscriptionResult,
)
from clear_record.providers import BackendBase, BackendInfo
from clear_record.service import Registry, RunManager

CHUNK_S = 3.0
OVERLAP_S = 1.0
TAPE_S = 8.0


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _tape(path: Path, seconds: float = TAPE_S) -> None:
    sr = 16000
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32), sr)


class CountingBackend(BackendBase):
    """One decode per chunk, each taking just long enough to be cancelled between.

    A real decode is a subprocess that runs for seconds or minutes; the delay
    stands in for that, so the run can be cancelled *between* chunks and the
    cache keeps whatever finished — which is exactly what the resume continues
    from.
    """

    info = BackendInfo(
        id="counting",
        vendor="test",
        frameworks=(),
        description="counts decodes",
        default_model="counting",
        parallelizable=True,
    )

    def __init__(self, seconds: float = 0.05) -> None:
        self.calls = 0
        self.seconds = seconds

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        self.calls += 1
        time.sleep(self.seconds)
        return TranscriptionResult(
            source="counting",
            segments=(Segment(0.0, 0.5, "words", "counting"),),
            language="en",
            backend="counting",
            model="counting",
            audio_duration=0.5,
        )


class SleepingChildBackend(BackendBase):
    """A decode that is a real child process, so a cancel has something to kill.

    The shape a real backend has: the decoder is a subprocess the pool launched
    through its runner. A cancel must reach *that* child — a chunk already
    decoding is where most of a long run's time goes, and a decoder that never
    returns would otherwise hold the run, its meeting and the node.
    """

    info = BackendInfo(
        id="child",
        vendor="test",
        frameworks=(),
        description="launches a child",
        default_model="child",
        parallelizable=True,
    )

    def __init__(self, sleep_s: int = 30) -> None:
        self.sleep_s = sleep_s
        self.started = threading.Event()

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        runner = kwargs["process_runner"]
        self.started.set()
        runner.run([sys.executable, "-c", f"import time; time.sleep({self.sleep_s})"])
        return TranscriptionResult(  # pragma: no cover - the cancel gets here first
            source="child",
            segments=(Segment(0.0, 0.5, "words", "child"),),
            language="en",
            backend="child",
            model="child",
            audio_duration=0.5,
        )


def _workspace(tmp_path) -> tuple[Path, Path]:
    """A meeting workspace with one ingested source; returns (dir, tape)."""
    directory = tmp_path / "ws"
    directory.mkdir()
    tape = directory / "a.wav"
    _tape(tape)
    stages.ingest(str(directory), split="mix")
    return directory, tape


def _meeting(registry: Registry, directory: Path, tape: Path):
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(directory))
    registry.set_recording_set(meeting.id, [str(tape)])
    return meeting


def _reporting_pipeline(directory, options, on_event, *, cancel=None) -> None:
    """Report until the run is cancelled: the shape a long stage has."""
    progress = Progress("transcribe", 1, on_event)
    progress.start()
    for _ in range(20_000):
        progress.advance(source="a")
        time.sleep(0.002)


def test_a_queued_run_is_cancelled_outright(tmp_path) -> None:
    """A queued cancel *is* the terminal transition: nothing may execute it.

    The node is busy with one run and a second meeting is waiting. Cancelling the
    waiting run moves it out of ``queued``, so the drain never picks it up, its
    meeting is free again, and a restart has nothing to resurrect.
    """
    registry = _registry(tmp_path)
    first_tape = tmp_path / "first.wav"
    second_tape = tmp_path / "second.wav"
    for tape in (first_tape, second_tape):
        tape.write_bytes(b"RIFFfake")
    first = _meeting(registry, tmp_path, first_tape)
    second = registry.create_meeting("ops", "Second", workspace_path=str(tmp_path))
    registry.set_recording_set(second.id, [str(second_tape)])

    ran: list[str] = []
    release = threading.Event()

    def pipeline(directory, options, on_event) -> None:
        ran.append(Path(options.audio_files[0]).name)
        release.wait(10)

    manager = RunManager(registry, pipeline=pipeline)
    running = manager.start(first, origin="console")
    for _ in range(1000):
        if registry.get_run(running.id).status == "running":
            break
        time.sleep(0.005)
    queued = manager.start(second, origin="console")
    assert registry.get_run(queued.id).status == "queued"

    stopped = manager.cancel(queued.id)

    assert stopped.status == "stopped"
    assert registry.get_run(queued.id).status == "stopped"
    # The options this manager kept for the run are dropped with it: a cancelled
    # run never executes, so nothing else would ever drop them.
    assert queued.id not in manager._pending
    # The meeting never started running, so a cancel leaves it runnable as it was.
    assert registry.meeting_by_id(second.id).status not in ("running", "recorded")

    release.set()
    assert manager.wait(running.id, timeout=10).status == "done"

    # A restart over the same registry leaves it stopped, and the drain never ran
    # it: the only tape that ever reached the pipeline is the first meeting's.
    restarted = RunManager(registry, pipeline=pipeline)
    try:
        assert manager.wait(queued.id, timeout=2.0).status == "stopped"
        assert registry.get_run(queued.id).status == "stopped"
    finally:
        restarted.shutdown(timeout=5)
    assert ran == ["first.wav"]


def test_a_cancel_mid_run_stops_at_the_next_report(tmp_path) -> None:
    """A running run stops where it next reports, and ends ``stopped``.

    Cancelled from another thread — the console's shape. The run queue hands the
    pipeline a channel whose signal makes the next report raise, so the pipeline
    unwinds from a safe point; the run keeps the cost record of what it did, and
    the meeting goes back to runnable, which is what a resume continues from.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, tape)

    manager = RunManager(registry, pipeline=_reporting_pipeline)
    run = manager.start(meeting, origin="console")
    # Wait for the first *persisted* report rather than for the claim: the queue's
    # channel raises before it records anything, so a cancel that wins the race
    # against the pipeline's first event leaves a run that stopped without
    # reporting, and the assertion below has no event to read.
    for _ in range(1000):
        if registry.count_run_events(run.id) > 0:
            break
        time.sleep(0.005)

    requested = manager.cancel(run.id)

    # A running run is *asked*: the row records the request, and the owner — this
    # manager — stops it at the next boundary.
    assert requested.status == "running"
    assert requested.cancel_requested_at is not None

    state = manager.wait(run.id, timeout=10)
    assert state.status == "stopped"

    row = registry.get_run(run.id)
    assert row.error is None  # a cancel is an outcome, not a failure
    assert row.progress is not None and "cost" in row.progress
    assert registry.meeting_by_id(meeting.id).status == "ready"
    assert state.events and state.events[-1].stage == "transcribe"


def test_a_cancel_stops_a_decode_that_is_already_running(tmp_path, monkeypatch) -> None:
    """A cancel reaches a decode in flight, not only the gap between chunks.

    Without this the cancel would wait for the chunk — for a whole-file backend
    that is the whole recording — and a decoder that never returns would leave
    the run non-terminal, its meeting refused and the node wedged behind it.
    """
    backend = SleepingChildBackend()
    monkeypatch.setattr(stages, "get_backend", lambda _id: backend)

    registry = _registry(tmp_path)
    directory, tape = _workspace(tmp_path)
    meeting = _meeting(registry, directory, tape)

    def pipeline(directory, options, on_event) -> None:
        stages.transcribe(
            directory,
            "child",
            chunk_seconds=CHUNK_S,
            overlap_seconds=OVERLAP_S,
            jobs=1,
            on_event=on_event,
        )

    manager = RunManager(registry, pipeline=pipeline)
    try:
        run = manager.start(meeting, origin="console")
        assert backend.started.wait(10), "the decode never started"
        assert manager.cancel(run.id) is not None
        # The child is killed where it stands, so the run ends well inside a
        # decode that would otherwise run for half a minute.
        assert manager.wait(run.id, timeout=10).status == "stopped"
        assert registry.get_run(run.id).status == "stopped"
        assert registry.meeting_by_id(meeting.id).status == "ready"
    finally:
        manager.shutdown(timeout=5)


def test_a_cancel_request_stops_a_run_another_writer_owns(
    tmp_path, monkeypatch
) -> None:
    """A second writer can stop a run executing in another process.

    The console relative to an agent's MCP server: the run belongs to whoever is
    executing it, so the other writer records a request rather than ending the
    row itself — its pipeline is mid-write in a workspace. The owner reads the
    request on its heartbeat and stops at its next report.
    """
    from clear_record.service import runs as runs_module

    monkeypatch.setattr(runs_module, "HEARTBEAT_INTERVAL_S", 0.01)

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, tape)

    owner = RunManager(registry, pipeline=_reporting_pipeline)
    run = owner.start(meeting, origin="console")
    for _ in range(1000):
        if registry.get_run(run.id).status == "running":
            break
        time.sleep(0.005)

    other = RunManager(registry, pipeline=lambda *args: None)
    try:
        requested = other.cancel(run.id)
        # Not this writer's to end: the row stays running, with the request on it.
        assert requested.status == "running"
        assert requested.cancel_requested_at is not None
        # The owner honours it, and its own terminal write is what lands.
        assert owner.wait(run.id, timeout=10).status == "stopped"
        assert registry.get_run(run.id).status == "stopped"
    finally:
        other.shutdown(timeout=5)
        owner.shutdown(timeout=5)


def test_a_resume_re_uses_the_cached_chunks(tmp_path, monkeypatch) -> None:
    """Resuming continues the work: the new run re-uses what the old one decoded.

    A real transcribe stage over a real workspace, so the numbers are the stage's
    own: the cancelled run left one chunk in the cache, the resumed run reports
    that chunk as reused and decodes the rest, and the two runs are linked.
    """
    backend = CountingBackend()
    monkeypatch.setattr(stages, "get_backend", lambda _id: backend)

    registry = _registry(tmp_path)
    directory, tape = _workspace(tmp_path)
    meeting = _meeting(registry, directory, tape)

    def pipeline(directory, options, on_event) -> None:
        stages.transcribe(
            directory,
            "counting",
            chunk_seconds=CHUNK_S,
            overlap_seconds=OVERLAP_S,
            resume=options.resume,
            jobs=1,
            on_event=on_event,
        )
        stages.reconcile(directory, on_event=on_event)
        stages.export(directory, on_event=on_event)

    manager = RunManager(registry, pipeline=pipeline)
    first = manager.start(meeting, origin="console")
    # Stop it while the chunks are still coming: one is cached, the rest are not.
    for _ in range(2000):
        if backend.calls >= 1 and registry.get_run(first.id).status == "running":
            break
        time.sleep(0.005)
    try:
        assert manager.cancel(first.id) is not None
        assert manager.wait(first.id, timeout=15).status == "stopped"
        assert backend.calls >= 1, "one chunk decoded before the stop"

        resumed = manager.resume(first.id, origin="console")
        state = manager.wait(resumed.id, timeout=30)
    finally:
        manager.shutdown(timeout=5)

    assert state.status == "done"
    assert registry.get_run(resumed.id).resumes_run_id == first.id
    cost = registry.get_run(resumed.id).progress["cost"]
    assert cost["chunks_reused"] >= 1, "the resumed run continued from the cache"
    assert cost["chunks_redecoded"] >= 1, "and decoded the chunks the stop left"
    assert cost["chunks"] == cost["chunks_reused"] + cost["chunks_redecoded"]


def test_a_resume_refuses_a_run_that_is_still_in_flight(tmp_path) -> None:
    """A live run is cancelled first, never resumed beside itself."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, tape)

    release = threading.Event()
    manager = RunManager(registry, pipeline=lambda *args: release.wait(10))
    run = manager.start(meeting, origin="console")
    try:
        with pytest.raises(ValueError):
            manager.resume(run.id, origin="console")
    finally:
        release.set()
        assert manager.wait(run.id, timeout=10).status == "done"
        manager.shutdown(timeout=5)
