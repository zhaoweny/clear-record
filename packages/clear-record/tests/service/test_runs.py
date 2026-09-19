"""The run manager: lifecycle, the live event stream, and artifact registration.

The pipeline callable is injected, so a full run — status transitions, progress
events, artifact checksums — is exercised with no ASR backend and no GPU.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from clear_record.cli.workspace import Workspace
from clear_record.core import JobEvent, Progress, resolve_options
from clear_record.service import (
    PipelineOptions,
    Registry,
    RunManager,
    estimate_eta_s,
    project_snapshot,
    snapshot_from_text,
)


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(registry: Registry, tmp_path, tapes: list[Path]):
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    registry.set_recording_set(meeting.id, [str(tape) for tape in tapes])
    return meeting


def test_run_defaults_to_the_project_glossary_snapshot(tmp_path) -> None:
    """A run with no explicit glossary applies the project's confirmed terms.

    This is the tuning-loop bridge: the registry's confirmed terms are written
    to the workspace ``glossary.txt`` and recorded (path + hash) with the run;
    an unreviewed candidate never reaches the decoder.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Falcon", status="confirmed")
    registry.add_term("ops", "Draft", added_by="agent")  # candidate
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options
        seen["text"] = Path(options.glossary).read_text(encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    manager.wait(run.id, timeout=10)

    assert seen["text"] == "Falcon\n"  # candidate excluded
    snapshot = project_snapshot(registry, "ops")
    assert run.options == {
        "profile": "custom",
        "decoder_knobs": {},
        "glossary": str(Workspace.at(meeting.workspace_path).glossary_path),
        "glossary_sha256": snapshot.sha256,
    }
    assert registry.get_run(run.id).options == run.options


def test_start_records_the_resolved_profile_knobs_and_auto_meta(tmp_path) -> None:
    """The run meta explains a finished run: resolved values, and what auto chose.

    ``profile`` and ``decoder_knobs`` are the **post-precedence** values (the
    profile's ``beam_size`` is recorded, not the requested preset alone), and the
    ``auto`` section carries the CLI's explanation verbatim.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=lambda *args: None)
    options = resolve_options(PipelineOptions(), profile="accurate")
    explanation = "--auto: chose profile 'accurate' (a short tape)"
    run = manager.start(
        meeting,
        options,
        auto={"auto": {"explanation": explanation, "chose": ["profile"]}},
        origin="console",
    )
    manager.wait(run.id, timeout=10)

    assert run.options["profile"] == "accurate"
    assert run.options["decoder_knobs"] == {"beam_size": 8}
    assert run.options["auto"]["explanation"] == explanation
    assert run.options["auto"]["chose"] == ["profile"]
    assert registry.get_run(run.id).options == run.options


def test_an_edited_glossary_changes_the_next_runs_recorded_hash(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Falcon", status="confirmed")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=lambda *args: None)
    first = manager.start(meeting, origin="console")
    manager.wait(first.id, timeout=10)

    registry.add_term("ops", "Booster", status="confirmed")
    second = manager.start(meeting, origin="console")
    manager.wait(second.id, timeout=10)

    assert first.options["glossary_sha256"] != second.options["glossary_sha256"]
    assert (
        Workspace.at(meeting.workspace_path).glossary_path.read_text(encoding="utf-8")
        == "Booster\nFalcon\n"
    )


def test_an_explicit_glossary_wins_over_the_project_snapshot(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Falcon", status="confirmed")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    explicit = tmp_path / "tuned.txt"
    explicit.write_text("# tuned\nCustom\n", encoding="utf-8")

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(
        meeting, PipelineOptions(glossary=str(explicit)), origin="console"
    )
    manager.wait(run.id, timeout=10)

    assert seen["options"].glossary == str(explicit)
    assert run.options["glossary"] == str(explicit)
    assert (
        run.options["glossary_sha256"]
        == snapshot_from_text(explicit.read_text(encoding="utf-8")).sha256
    )
    # No project snapshot was written — the explicit file won.
    assert not Workspace.at(meeting.workspace_path).glossary_path.exists()


def test_a_hand_written_glossary_survives_when_there_are_no_confirmed_terms(
    tmp_path,
) -> None:
    """With no confirmed terms the registry has nothing to say: the user's file stands.

    ``glossary.txt`` is a documented, user-editable artifact (``clear-record
    glossary`` / the README), so an empty registry must not clobber it — and the
    run uses the surviving file, recording its identity.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Draft", added_by="agent")  # candidate only
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    workspace = Workspace.at(meeting.workspace_path)
    workspace.glossary_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.glossary_path.write_text("UserTerm\n", encoding="utf-8")

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    manager.wait(run.id, timeout=10)

    # Untouched, and the run's glossary is that file (no explicit path set, so
    # the pipeline reads the workspace default).
    assert workspace.glossary_path.read_text(encoding="utf-8") == "UserTerm\n"
    assert seen["options"].glossary is None
    assert run.options == {
        "profile": "custom",
        "decoder_knobs": {},
        "glossary": str(workspace.glossary_path),
        "glossary_sha256": snapshot_from_text("UserTerm\n").sha256,
    }


def test_confirmed_terms_write_and_win_over_a_hand_written_glossary(tmp_path) -> None:
    """Once the registry has confirmed terms it is authoritative and overwrites."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Falcon", status="confirmed")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    workspace = Workspace.at(meeting.workspace_path)
    workspace.glossary_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.glossary_path.write_text("UserTerm\n", encoding="utf-8")

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    manager.wait(run.id, timeout=10)

    assert workspace.glossary_path.read_text(encoding="utf-8") == "Falcon\n"
    assert seen["options"].glossary == str(workspace.glossary_path)
    assert run.options == {
        "profile": "custom",
        "decoder_knobs": {},
        "glossary": str(workspace.glossary_path),
        "glossary_sha256": project_snapshot(registry, "ops").sha256,
    }


def test_run_lifecycle_records_events_and_artifacts(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def fake_pipeline(directory, options, on_event) -> None:
        assert options.audio_files == (str(tape),)
        progress = Progress("transcribe", 2, on_event)
        progress.start()
        progress.advance(source="a")
        progress.advance(source="a")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")
        (Path(directory) / "record.json").write_text("{}", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    state = manager.wait(run.id, timeout=10)

    assert state.status == "done"
    assert [event.index for event in state.events] == [0, 1, 2]
    assert state.events[-1].done
    assert registry.get_run(run.id).status == "done"
    assert registry.meeting_by_id(meeting.id).status == "recorded"

    artifacts = registry.list_artifacts(meeting.id)
    assert {artifact.kind for artifact in artifacts} == {"record", "export"}
    assert all(artifact.sha256 for artifact in artifacts)
    assert all(artifact.bytes for artifact in artifacts)


def test_wait_does_not_return_until_the_run_leaves_live(tmp_path) -> None:
    """``wait`` ends only when the run is terminal *and* off the live set.

    The registry flips to ``done`` inside ``_run_pipeline``, but the worker keeps
    the id in ``_live`` until that method and its synchronous effects (the
    artifact rows and the ``run.finished`` log) are complete. Returning on the
    status alone opens a window where a waiter sees a finished run before its
    ``run.finished`` log exists — the race that made
    ``test_run_lifecycle_is_logged`` flaky. This freezes that window: the run
    reaches ``done`` while it is still live, and stays there.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    frozen = threading.Event()
    release = threading.Event()

    class FrozenAfterTerminal(RunManager):
        def _run_pipeline(self, run, meeting, options) -> None:
            super()._run_pipeline(run, meeting, options)
            frozen.set()
            release.wait(10)

    manager = FrozenAfterTerminal(registry, pipeline=lambda *args: None)
    run = manager.start(meeting, origin="console")
    assert frozen.wait(10), "the run did not reach its terminal transition"
    assert manager.require_state(run.id).status == "done"
    with manager._lock:
        assert run.id in manager._live

    # The status is already terminal; the old wait would return here. It must
    # instead block until the timeout, then return the state (the timeout rule).
    started = time.monotonic()
    state = manager.wait(run.id, timeout=0.5)
    elapsed = time.monotonic() - started
    assert state.status == "done"
    assert elapsed >= 0.45, elapsed

    # Once the worker lets go, wait returns promptly rather than waiting out
    # the whole timeout.
    release.set()
    started = time.monotonic()
    state = manager.wait(run.id, timeout=10)
    assert state.status == "done"
    assert time.monotonic() - started < 5


def test_failed_run_is_recorded(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def boom(directory, options, on_event) -> None:
        raise RuntimeError("backend unavailable")

    manager = RunManager(registry, pipeline=boom)
    state = manager.wait(manager.start(meeting, origin="console").id, timeout=10)

    assert state.status == "failed"
    assert "backend unavailable" in (state.error or "")
    assert registry.get_run(state.run_id).status == "failed"
    assert registry.meeting_by_id(meeting.id).status == "failed"
    assert registry.list_artifacts(meeting.id) == []


def test_a_failure_around_the_pipeline_still_fails_the_meeting(tmp_path) -> None:
    """An error outside the pipeline's own try must not leave the meeting running."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=lambda *args: None)

    def boom(meeting, run_id):
        raise RuntimeError("artifact registration exploded")

    manager._register_artifacts = boom  # type: ignore[method-assign]

    state = manager.wait(manager.start(meeting, origin="console").id, timeout=10)

    assert state.status == "failed"
    assert registry.meeting_by_id(meeting.id).status == "failed"


def test_run_requires_a_workspace_and_a_tape_set(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    manager = RunManager(registry, pipeline=lambda *args: None)

    no_tapes = registry.create_meeting("ops", "No tapes", workspace_path=str(tmp_path))
    with pytest.raises(ValueError):
        manager.start(no_tapes, origin="console")

    no_workspace = registry.create_meeting("ops", "No workspace")
    registry.set_recording_set(no_workspace.id, [str(tmp_path / "x.wav")])
    with pytest.raises(ValueError):
        manager.start(no_workspace, origin="console")


def test_a_second_run_is_refused_while_one_is_in_flight(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    release = threading.Event()

    def slow_pipeline(directory, options, on_event) -> None:
        release.wait(10)

    manager = RunManager(registry, pipeline=slow_pipeline)
    run = manager.start(meeting, origin="console")
    with pytest.raises(ValueError):
        manager.start(meeting, origin="console")

    release.set()
    assert manager.wait(run.id, timeout=10).status == "done"


def test_event_cursor_supports_streaming(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def fake_pipeline(directory, options, on_event) -> None:
        progress = Progress("ingest", 3, on_event)
        progress.start()
        for _ in range(3):
            progress.advance()

    manager = RunManager(registry, pipeline=fake_pipeline)
    state = manager.wait(manager.start(meeting, origin="console").id, timeout=10)

    assert len(state.events_since(0)) == 4
    assert state.events_since(4) == []
    assert state.summary()["total"] == 3
    assert state.summary()["status"] == "done"


# --- durability across a restart ------------------------------------------- #


def test_events_persist_and_replay(tmp_path) -> None:
    """A run's event stream is durable: a later manager replays it."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def fake_pipeline(directory, options, on_event) -> None:
        progress = Progress("transcribe", 2, on_event)
        progress.start("transcribing")
        progress.advance(source="a")
        progress.advance(source="b")

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    manager.wait(run.id, timeout=10)

    # A *new* manager over the same registry has no process memory of the run...
    restarted = RunManager(registry, pipeline=lambda *args: None)
    state = restarted.require_state(run.id)
    assert state.status == "done"
    assert [event.index for event in state.events] == [0, 1, 2]


def test_a_running_run_from_a_dead_process_becomes_interrupted(tmp_path) -> None:
    """Startup reconciliation is honest: running -> interrupted, with the reason."""
    from clear_record.core import JobEvent
    from clear_record.service import RESTART_REASON

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    # A run left mid-flight by a process that died.
    orphan = registry.create_run(meeting.id, backend="apple")
    registry.update_run(
        orphan.id, status="running", started_at="2026-01-01T00:00:00+00:00"
    )
    registry.set_meeting_status(meeting.id, "running")
    registry.add_run_event(orphan.id, JobEvent(stage="ingest", index=1, total=3))
    registry.add_run_event(orphan.id, JobEvent(stage="transcribe", index=0, total=2))

    manager = RunManager(registry, pipeline=lambda *args: None)

    reconciled = registry.get_run(orphan.id)
    assert reconciled.status == "interrupted"
    assert reconciled.error == RESTART_REASON
    assert registry.meeting_by_id(meeting.id).status == "interrupted"

    state = manager.require_state(orphan.id)
    assert state.status == "interrupted"
    assert [event.stage for event in state.events] == ["ingest", "transcribe"]

    # The honesty requirement: a new run is startable after the restart.
    fresh = manager.start(meeting, origin="console")
    assert manager.wait(fresh.id, timeout=10).status == "done"
    assert registry.meeting_by_id(meeting.id).status == "recorded"


def test_a_queued_run_survives_a_restart_and_is_drained(tmp_path) -> None:
    """Queued work is persisted, with its options, and picked back up."""
    import dataclasses

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    queued = registry.create_run(
        meeting.id,
        backend="apple",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options

    # A restart: a fresh manager over the same registry drains the queue.
    manager = RunManager(registry, pipeline=fake_pipeline)
    state = manager.wait(queued.id, timeout=10)

    assert state.status == "done"
    assert seen["options"].audio_files == (str(tape),)


# --- the node queue: one run at a time ------------------------------------- #


def test_two_meetings_queue_instead_of_fighting_for_the_node(tmp_path) -> None:
    """Later meetings wait their turn; only one executes at a time (FIFO)."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tapes = [tmp_path / f"{name}.wav" for name in ("first", "second", "third")]
    for tape in tapes:
        tape.write_bytes(b"RIFFfake")
    meetings = []
    for tape, name in zip(tapes, ("First", "Second", "Third")):
        meeting = registry.create_meeting("ops", name, workspace_path=str(tmp_path))
        registry.set_recording_set(meeting.id, [str(tape)])
        meetings.append(meeting)

    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    order: list[str] = []

    def fake_pipeline(directory, options, on_event) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            order.append(options.audio_files[0])
        release.wait(10)
        with lock:
            active -= 1

    manager = RunManager(registry, pipeline=fake_pipeline)
    runs = [manager.start(meeting, origin="console") for meeting in meetings]

    # Wait until the first is actually executing; the rest must be queued, in
    # FIFO order, reporting their place in the wait line (1 = next).
    for _ in range(1000):
        if order:
            break
        time.sleep(0.005)

    assert registry.get_run(runs[0].id).status == "running"
    assert manager.require_state(runs[1].id).status == "queued"
    assert manager.require_state(runs[1].id).position == 1
    assert manager.require_state(runs[2].id).position == 2

    release.set()
    for run in runs:
        assert manager.wait(run.id, timeout=10).status == "done"

    assert peak == 1  # one run per node
    assert order == [str(tape) for tape in tapes]  # FIFO


def test_shutdown_is_bounded_and_does_not_cancel_a_run(tmp_path) -> None:
    """A clean stop signals the queue and returns; it never blocks on the run."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    release = threading.Event()
    manager = RunManager(registry, pipeline=lambda *args: release.wait(10))
    run = manager.start(meeting, origin="console")
    for _ in range(1000):
        if manager.require_state(run.id).status == "running":
            break
        time.sleep(0.005)

    started = time.monotonic()
    manager.shutdown(timeout=0.0)
    assert time.monotonic() - started < 1.0  # did not wait for the pipeline

    release.set()
    assert manager.wait(run.id, timeout=10).status == "done"


def test_the_active_guard_is_derived_from_the_registry(tmp_path) -> None:
    """A run persisted by another process still blocks a second for the meeting."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    # A run row the manager did not see start (its own earlier process), already
    # terminal? No — running, so reconciliation moves it and frees the meeting.
    orphan = registry.create_run(meeting.id, backend="apple")
    registry.update_run(orphan.id, status="running", started_at="now")
    release = threading.Event()
    manager = RunManager(registry, pipeline=lambda *args: release.wait(10))
    assert manager.active_state(meeting.id) is None  # reconciled to interrupted

    # A queued run the registry holds (no manager.start) is still the guard.
    fresh = registry.create_run(
        meeting.id,
        backend="apple",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    state = manager.active_state(meeting.id)
    assert state is not None and state.run_id == fresh.id
    with pytest.raises(ValueError):
        manager.start(meeting, origin="console")

    release.set()
    assert manager.wait(fresh.id, timeout=10).status == "done"


class _UnreadableBeats:
    """A registry whose beat read fails the way a locked database does.

    Everything else is the wrapped registry — the manager claims, drains and reads
    through it unchanged. ``heartbeat_run`` reports that the beat did not land, so
    the heartbeat thread goes on to read the row (``get_run``), which is where this
    fake raises while ``raise_on_read`` is set.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self.beats = 0
        self.raise_on_read = True

    def heartbeat_run(self, run_id: int, *, at: str | None = None) -> bool:
        self.beats += 1
        return False

    def get_run(self, run_id: int):
        if self.raise_on_read:
            raise sqlite3.OperationalError("database is locked")
        return self._registry.get_run(run_id)

    def __getattr__(self, name):
        return getattr(self._registry, name)


# --- the claim: one queue, every writer (RUN-02) ---------------------------- #

#: The child a crash or stall test runs: it claims and executes a run and then
#: blocks, so the parent can freeze it (``SIGSTOP``) or kill it. Either way the row
#: is left ``running`` — with a stale beat and a live pid, or a fresh beat and no
#: pid at all — which is exactly what a stalled or killed writer leaves behind.
_BLOCKED_OWNER = '''
import os
import pathlib
import sys
import time

from clear_record.service import Registry, RunManager, WebhookEmitter

db_path, marker = sys.argv[1:3]

def pipeline(directory, options, on_event):
    """Never returns: the parent freezes or kills this process mid-run."""
    pathlib.Path(marker).write_text(str(os.getpid()))
    time.sleep(120)

# An inert emitter, so a child never reads the machine's real webhook config.
RunManager(
    Registry.open(db_path=db_path), pipeline=pipeline, webhooks=WebhookEmitter(())
)
time.sleep(120)
'''


def _child_env(tmp_path) -> dict:
    """The environment a spawned child gets: the app-owned dirs inside the test.

    A child inherits the environment, not the suite's in-process redirection (see
    ``conftest``), so without this a spawned manager would resolve — and write —
    the machine's real data, log and cache directories.
    """
    return {
        **os.environ,
        "CR_DATA_DIR": str(tmp_path / "child-data"),
        "CR_STATE_DIR": str(tmp_path / "child-state"),
        "CR_LOG_DIR": str(tmp_path / "child-logs"),
        "CR_CACHE_DIR": str(tmp_path / "child-cache"),
    }


#: Names the throwaway child scripts. Unique per call, because two children in one
#: test must never share a file: a second write could land while the first child
#: is still reading it.
_CHILD_SCRIPTS = itertools.count()


def _run_child(tmp_path, source: str, *args: str) -> subprocess.Popen:
    """Write a small script and start it as a child process, hermetically."""
    script = tmp_path / f"child-{next(_CHILD_SCRIPTS)}.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(tmp_path),
    )


#: The child a two-process claim race runs: it constructs its manager — and so
#: starts draining the shared queue — only once both children are at the gate,
#: records the run's work with the pid that did it, and reports what it saw.
_TWO_PROCESS_DRAIN = '''
import json
import os
import pathlib
import sys
import time

from clear_record.service import Registry, RunManager, WebhookEmitter

db_path, gate, ready, result, executed_log, run_id = sys.argv[1:7]
gate, ready = pathlib.Path(gate), pathlib.Path(ready)
executed = False


def pipeline(directory, options, on_event):
    """The run's work, recorded by the process that actually did it."""
    global executed
    executed = True
    with pathlib.Path(executed_log).open("a") as handle:
        handle.write(f"{os.getpid()}\\n")


ready.write_text("")
while not gate.exists():
    time.sleep(0.002)

registry = Registry.open(db_path=db_path)
# An inert emitter: this child must not read the machine's real webhook config,
# let alone deliver to it (the parent's own process is made hermetic by the
# suite's fixture, and a child inherits only environment variables).
manager = RunManager(registry, pipeline=pipeline, webhooks=WebhookEmitter(()))
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    row = registry.get_run(int(run_id))
    if row is not None and row.status in ("done", "failed", "interrupted"):
        break
    time.sleep(0.02)
manager.shutdown(timeout=5)
row = registry.get_run(int(run_id))
pathlib.Path(result).write_text(
    json.dumps(
        {
            "pid": os.getpid(),
            "executed": executed,
            "status": row.status if row else None,
            "owner": row.owner if row else None,
        }
    )
)
'''


def test_the_claim_is_a_conditional_update(tmp_path) -> None:
    """The move to ``running`` is one conditional update: one winner, one loser.

    The loser's update matches no row, so it writes nothing — no status, no
    owner, no ``started_at`` — and is told so, where :meth:`Registry.update_run`
    on the same mismatch raises ``KeyError``. The same statement carries the
    node's one-run-at-a-time rule, so a second queued run cannot be claimed while
    the first is running.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    queued = registry.create_run(meeting.id, origin="console")
    waiting = registry.create_run(
        registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path)).id,
        origin="mcp",
    )

    winner = registry.claim_run(queued.id, owner="console:1")
    assert winner is not None
    assert (winner.status, winner.owner) == ("running", "console:1")
    assert winner.started_at is not None and winner.heartbeat_at is not None

    # The same run, claimed again: no row matches and nothing is overwritten.
    assert registry.claim_run(queued.id, owner="mcp:2") is None
    still = registry.get_run(queued.id)
    assert (still.status, still.owner) == ("running", "console:1")

    # One run per node: a *different* queued run is not claimable either.
    assert registry.claim_run(waiting.id, owner="mcp:2") is None
    assert registry.get_run(waiting.id).status == "queued"

    # The heartbeat refreshes a running run and stops when it is no longer one.
    assert registry.heartbeat_run(queued.id, at="2026-01-01T00:00:00+00:00")
    assert registry.get_run(queued.id).heartbeat_at == "2026-01-01T00:00:00+00:00"
    registry.update_run(queued.id, status="done")
    assert registry.heartbeat_run(queued.id) is False
    assert registry.get_run(queued.id).heartbeat_at == "2026-01-01T00:00:00+00:00"

    # The node is free again, so the run that was waiting is claimable.
    assert registry.claim_run(waiting.id, owner="mcp:2") is not None


def test_a_contended_claim_waits_and_still_wins(tmp_path) -> None:
    """A claim meeting another writer waits for the lock instead of failing.

    The claim is the queue's hinge, so "database is locked" under contention
    would mean either a stranded run or a second executor. SQLite refuses
    *without waiting* only a writer that has to upgrade a read transaction, which
    is the other reason the claim is one statement with no read before it; the
    connection's busy timeout covers the rest. Here another connection holds the
    write lock for the whole claim.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    queued = registry.create_run(meeting.id, origin="console")

    holding = threading.Event()

    def hold_the_write_lock() -> None:
        holder = sqlite3.connect(str(registry.db_path))
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE schema_version SET version = version")
            holding.set()
            time.sleep(0.3)
        finally:
            holder.rollback()
            holder.close()

    holder_thread = threading.Thread(target=hold_the_write_lock)
    holder_thread.start()
    assert holding.wait(5)
    started = time.monotonic()
    claimed = registry.claim_run(queued.id, owner="console:1")
    waited = time.monotonic() - started
    holder_thread.join(5)

    assert claimed is not None
    assert waited >= 0.2  # it waited for the writer rather than raising


def test_a_run_records_the_surface_that_started_it(tmp_path) -> None:
    """RUN-02: origin is written at enqueue, survives the run, and is validated.

    ``origin`` is required on the start path (a caller cannot forget it), the
    registry refuses a value outside :data:`RUN_ORIGINS`, and the value is read
    back from the row — so a run started by an agent's MCP server still says so
    after the process that started it is gone.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=lambda *args: None)
    run = manager.start(meeting, origin="mcp")
    assert registry.get_run(run.id).origin == "mcp"
    assert manager.wait(run.id, timeout=10).status == "done"
    assert registry.get_run(run.id).origin == "mcp"

    with pytest.raises(ValueError):
        manager.start(meeting, origin="grafana")
    with pytest.raises(TypeError):
        manager.start(meeting)  # type: ignore[call-arg] - a start path names itself


def test_reconciliation_respects_a_live_peer_and_reaps_a_dead_one(tmp_path) -> None:
    """A heartbeat its owner keeps fresh protects a run; a stale one does not.

    This is what makes two writers safe (RUN-02). A second process must not read
    the first's in-flight run as an orphan and interrupt it — the row is owned by
    a peer whose heartbeat is fresh, so reconciliation leaves it and the queue
    stays honestly busy. A run whose owner stopped reporting (a process that was
    killed) is still reaped, and the work queued behind it moves on.
    """
    from clear_record.service import RESTART_REASON

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    busy = _meeting(registry, tmp_path, [tape])
    waiting = registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path))
    registry.set_recording_set(waiting.id, [str(tape)])

    # A peer process claimed this run and is executing it: the claim wrote a
    # heartbeat, and nothing else has touched the row since.
    peer_run = registry.create_run(
        busy.id,
        origin="mcp",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    # The owner is one the pid probe cannot resolve, so the **heartbeat** is
    # what decides here: an owner that looks like a live pid would hold the
    # run on a machine that happens to have that pid, and the test would be
    # about the probe instead of about the beat.
    assert registry.claim_run(peer_run.id, owner="peer:not-a-pid") is not None
    registry.set_meeting_status(busy.id, "running")
    behind = registry.create_run(
        waiting.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        # The peer is alive, so its run is not an orphan and the node stays busy.
        # Two seconds is more than the drain loop's tick: a reap would have shown.
        assert manager.wait(behind.id, timeout=2.0).status == "queued"
        untouched = registry.get_run(peer_run.id)
        assert untouched.status == "running" and untouched.error is None

        # The peer dies without a word: its heartbeat is all that is left of it,
        # and once it is stale the queue takes over.
        assert registry.heartbeat_run(peer_run.id, at="2020-01-01T00:00:00+00:00")
        assert manager.wait(behind.id, timeout=10).status == "done"
    finally:
        manager.shutdown(timeout=5)

    reaped = registry.get_run(peer_run.id)
    assert (reaped.status, reaped.error) == ("interrupted", RESTART_REASON)
    assert registry.meeting_by_id(busy.id).status == "interrupted"


@pytest.mark.skipif(
    os.name != "posix", reason="the owner probe needs POSIX kill(pid, 0)"
)
def test_a_run_whose_owner_is_alive_holds_its_meeting_and_the_node(tmp_path) -> None:
    """A stalled owner holds the queue: a live pid outranks a stale heartbeat.

    The owner here is a live process on this host — this test process, named the
    way the claim names one — and its heartbeat is years stale, which is the shape
    of a stalled owner. The run must still be ``running``, because reaping it
    would free *both* guards: a second pipeline for the same meeting and a second
    run on the node would be admitted while the first one still transcribes.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    busy = _meeting(registry, tmp_path, [tape])
    waiting = registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path))
    registry.set_recording_set(waiting.id, [str(tape)])

    held = registry.create_run(
        busy.id,
        origin="mcp",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    owner = f"{platform.node()}:{os.getpid()}"
    assert registry.claim_run(held.id, owner=owner) is not None
    registry.set_meeting_status(busy.id, "running")
    assert registry.heartbeat_run(held.id, at="2020-01-01T00:00:00+00:00")
    behind = registry.create_run(
        waiting.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        assert manager.reconcile() == []  # a process exists: not an orphan
        holds = registry.get_run(held.id)
        assert holds.status == "running" and holds.error is None
        # The node and the meeting are still held, ticks and all.
        assert manager.wait(behind.id, timeout=2.0).status == "queued"
        with pytest.raises(ValueError):
            manager.start(busy, origin="console")
    finally:
        manager.shutdown(timeout=5)


@pytest.mark.skipif(
    os.name != "posix", reason="the owner probe needs POSIX kill(pid, 0)"
)
def test_a_killed_owner_is_reaped_at_once(tmp_path) -> None:
    """A run whose owner process is gone is an orphan now, not in 30 s (RUN-02).

    A killed console leaves a heartbeat a second or two old, so the heartbeat
    alone would keep its run — and its meeting — refused for the whole deadline,
    which is not the reconciliation the queue has always promised. The owner is
    written as ``host:pid``, so the next process asks the OS instead: the pid is
    gone, the run is interrupted, and the work queued behind it moves on.
    """
    import datetime as dt

    from clear_record.service import HEARTBEAT_STALE_S, RESTART_REASON

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    waiting = registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path))
    registry.set_recording_set(waiting.id, [str(tape)])
    run = registry.create_run(
        meeting.id,
        origin="mcp",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    behind = registry.create_run(
        waiting.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    marker = tmp_path / "running"
    child = _run_child(tmp_path, _BLOCKED_OWNER, str(registry.db_path), str(marker))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        started = marker.exists()
        if started:
            running = registry.get_run(run.id)
            assert running.status == "running"
            assert running.owner == f"{platform.node()}:{marker.read_text()}"
        child.kill()
        _, stderr = child.communicate(timeout=30)
        assert started, f"the child never started its run: {stderr}"
    finally:
        child.kill()
        child.wait(timeout=30)

    # The heartbeat is seconds old: the beat alone would read as a live owner.
    beat = dt.datetime.fromisoformat(running.heartbeat_at or "")
    age = (dt.datetime.now(dt.UTC) - beat).total_seconds()
    assert age < HEARTBEAT_STALE_S

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        reaped = registry.get_run(run.id)
        assert (reaped.status, reaped.error) == ("interrupted", RESTART_REASON)
        assert registry.meeting_by_id(meeting.id).status == "interrupted"
        assert manager.wait(behind.id, timeout=10).status == "done"
    finally:
        manager.shutdown(timeout=5)


@pytest.mark.skipif(
    os.name != "posix", reason="the owner probe needs POSIX kill(pid, 0)"
)
def test_a_frozen_owner_does_not_admit_a_second_pipeline(tmp_path, monkeypatch) -> None:
    """A stalled-but-alive owner holds the node: the blocker, with real processes.

    A real writer is frozen mid-run (``SIGSTOP``: its heartbeats really stop, and
    the deadline is shortened here so the stall is genuinely stale), and a second
    writer then starts over the same registry. The run must stay ``running`` and
    the meeting must stay refused: its owner process exists, so there is nothing
    to reconcile, and no second pipeline may be admitted beside it.
    """
    from clear_record.service import runs as runs_module

    monkeypatch.setattr(runs_module, "HEARTBEAT_STALE_S", 0.5)

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    busy = _meeting(registry, tmp_path, [tape])
    waiting = registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path))
    registry.set_recording_set(waiting.id, [str(tape)])
    run = registry.create_run(
        busy.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    behind = registry.create_run(
        waiting.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    marker = tmp_path / "running"
    child = _run_child(tmp_path, _BLOCKED_OWNER, str(registry.db_path), str(marker))
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        assert marker.exists(), "the child never started its run"
        owner_pid = int(marker.read_text())
        os.kill(owner_pid, signal.SIGSTOP)
        # The beat interval is 2 s, so this outlives the child's last heartbeat by
        # more than the (shortened) deadline: a stale beat and a live process.
        time.sleep(2.2)

        manager = RunManager(registry, pipeline=lambda *args: None)
        try:
            assert manager.reconcile() == []  # the owner exists: not an orphan
            held = registry.get_run(run.id)
            assert held.status == "running" and held.error is None
            assert manager.wait(behind.id, timeout=2.0).status == "queued"
            with pytest.raises(ValueError):
                manager.start(busy, origin="console")
        finally:
            manager.shutdown(timeout=5)
    finally:
        os.kill(child.pid, signal.SIGCONT)
        child.kill()
        child.wait(timeout=30)


def test_a_live_pid_holds_a_run_even_when_the_owner_names_another_host(
    tmp_path,
) -> None:
    """A host text that no longer matches must not free a live owner's run.

    ``platform.node()`` follows the machine's network name, so it can differ
    between the process that claimed a run and the process reconciling it (a VPN
    switch, a rename, a container) — and a shared registry would name another
    machine outright. A mismatch is not evidence that nobody is executing the run:
    the pid is alive here, and reaping on that basis would admit a second
    pipeline for the meeting beside a stalled-but-live owner. The run is claimed
    with a live pid under an unrecognized host name, which is that shape.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    busy = _meeting(registry, tmp_path, [tape])
    held = registry.create_run(
        busy.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    assert (
        registry.claim_run(
            held.id, owner=f"a-name-this-machine-had-before:{os.getpid()}"
        )
        is not None
    )
    registry.set_meeting_status(busy.id, "running")
    assert registry.heartbeat_run(held.id, at="2020-01-01T00:00:00+00:00")

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        assert manager.reconcile() == []
        assert registry.get_run(held.id).status == "running"
        with pytest.raises(ValueError):
            manager.start(busy, origin="console")
    finally:
        manager.shutdown(timeout=5)


def test_a_running_row_with_an_out_of_range_pid_reconciles(tmp_path) -> None:
    """An owner this node cannot parse is an orphan, not a crash.

    ``os.kill`` takes a C int, so a pid with too many digits raises
    ``OverflowError`` — a shape neither the parse guard nor the ``OSError`` guard
    covers. Reconciliation reaches it from a manager's *constructor* and from the
    drain loop, so an escape would stop the console from starting at all and, in
    a running console, kill the queue thread for good. The owner column is free
    text written by ``claim_run``, so this is a row the registry can hold.
    """
    from clear_record.service import RESTART_REASON

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(meeting.id, origin="console")
    # The owner column is free text and this pid is too large for C's int, which
    # is what `os.kill` converts to.
    oversized = f"{platform.node()}:{'9' * 24}"
    assert registry.claim_run(run.id, owner=oversized) is not None
    assert registry.heartbeat_run(run.id, at="2020-01-01T00:00:00+00:00")

    manager = RunManager(registry, pipeline=lambda *args: None)  # must not raise
    try:
        reaped = registry.get_run(run.id)
        assert (reaped.status, reaped.error) == ("interrupted", RESTART_REASON)
    finally:
        manager.shutdown(timeout=5)


def test_a_completed_run_logs_no_heartbeat_failure(tmp_path, monkeypatch) -> None:
    """Our own completion must not read as a peer reaping us.

    The terminal status lands a moment before the run leaves the manager's live
    set, so a beat can meet a row that is no longer ``running`` because *this*
    manager finished it. The interval is shortened here so those windows are hit
    many times per run; the warning is meant for a peer's reap and must stay
    silent.
    """
    from clear_record.core import read_recent
    from clear_record.service import runs as runs_module

    monkeypatch.setattr(runs_module, "HEARTBEAT_INTERVAL_S", 0.001)

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=lambda *args: None)
    for _ in range(3):
        run = manager.start(meeting, origin="console")
        assert manager.wait(run.id, timeout=10).status == "done"

    events = [json.loads(line)["event"] for line in read_recent(200)]
    assert "run.finished" in events  # the log is live, so this is not vacuous
    assert "run.heartbeat_failed" not in events


def test_a_failing_beat_read_does_not_kill_the_heartbeat_thread(
    tmp_path, monkeypatch
) -> None:
    """A read that fails while checking a beat must not stop the beats.

    The two registry calls of one beat are one job. Where an owner's pid cannot
    decide (an unparseable or absent owner, a registry shared with a platform
    without the probe, a row naming another machine) the beat is the *only*
    evidence a run is alive, so a thread that dies on a read error means a
    still-executing run reads as dead a deadline later — and its meeting and the
    node are freed, which is the admission the fail-closed rule exists to prevent.
    """
    from clear_record.service import runs as runs_module

    monkeypatch.setattr(runs_module, "HEARTBEAT_INTERVAL_S", 0.01)

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    flaky = _UnreadableBeats(registry)
    release = threading.Event()
    manager = RunManager(flaky, pipeline=lambda *args: release.wait(10))
    run = manager.start(meeting, origin="console")
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and flaky.beats < 3:
            time.sleep(0.005)
        assert flaky.beats >= 3  # a thread that died on the read beat exactly once
        assert registry.get_run(run.id).status == "running"
    finally:
        flaky.raise_on_read = False
        release.set()
        manager.wait(run.id, timeout=10)
        manager.shutdown(timeout=5)


def test_a_finished_run_does_not_keep_a_reapers_reason(tmp_path) -> None:
    """A successful run reports no error, whatever a reaper wrote meanwhile.

    A reaper can mark a run interrupted while its owner is in fact about to
    finish (it could not see that process). The owner's own record is the last
    word: a run that says ``done`` must not also carry the reap reason, which the
    console's run view and the API would otherwise show as a failure.
    """
    from clear_record.service import RESTART_REASON

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(meeting.id, origin="console")
    # The owner is one the pid probe cannot resolve, so the **heartbeat** is
    # what decides here: an owner that looks like a live pid would hold the
    # run on a machine that happens to have that pid, and the test would be
    # about the probe instead of about the beat.
    assert registry.claim_run(run.id, owner="peer:not-a-pid") is not None
    reaped = registry.interrupt_run(
        run.id,
        observed=registry.get_run(run.id),  # nothing beat it: the compare wins
        ended_at="2026-01-01T00:00:00+00:00",
        error=RESTART_REASON,
        progress={"interrupted": True},
    )
    assert reaped is not None and reaped.error == RESTART_REASON

    finished = registry.update_run(
        run.id,
        status="done",
        ended_at="2026-01-01T00:01:00+00:00",
        progress={"kept": 1},
    )
    assert (finished.status, finished.error) == ("done", None)
    assert finished.progress == {"kept": 1}
    assert registry.get_run(run.id).error is None


def test_a_second_manager_leaves_a_live_run_alone(tmp_path) -> None:
    """A second manager — another writer — does not reap a run that is live here.

    Both the live set and the heartbeat thread are per manager, so the second
    manager's startup has only the row to go on: the claim's own heartbeat must
    already be fresh, or an in-flight run would look dead for the few seconds
    before the first beat. This is also the case that breaks if reconciliation
    read "the owner is this process" as "a previous life of this process": two
    managers in one process share that identity.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    release = threading.Event()
    first = RunManager(registry, pipeline=lambda *args: release.wait(10))
    run = first.start(meeting, origin="console")
    for _ in range(1000):
        if registry.get_run(run.id).status == "running":
            break
        time.sleep(0.005)

    second = RunManager(registry, pipeline=lambda *args: None)
    live = registry.get_run(run.id)
    assert live.status == "running" and live.error is None
    second.shutdown(timeout=5)

    release.set()
    assert first.wait(run.id, timeout=10).status == "done"


def test_a_reap_cannot_overwrite_a_run_that_finished(tmp_path, monkeypatch) -> None:
    """The orphan transition is conditional: a finished run is not an orphan.

    A reaper decides from a **snapshot** — the heartbeat that had gone stale — and
    writes after it, and in between the owner can finish and record its own
    outcome. Staging that gap here (the snapshot is passed in directly) pins the
    precedence: the owner's record wins, so the row keeps ``done``, its progress
    and its cost record, and the meeting keeps the status that run gave it.

    The owner is one the pid probe cannot resolve as live, so the reap really runs
    and ``interrupt_run``'s conditional guard — the thing under test — is the call
    that decides: with a live pid the row would be held and the guard never
    reached, and this test would pass even if the guard were removed.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(meeting.id, origin="console")
    unreadable = f"{platform.node()}:{'9' * 24}"
    assert registry.claim_run(run.id, owner=unreadable) is not None
    assert registry.heartbeat_run(run.id, at="2020-01-01T00:00:00+00:00")

    stale = registry.get_run(run.id)  # what a reaper decided from
    registry.update_run(
        run.id,
        status="done",
        ended_at="2026-01-01T00:00:00+00:00",
        progress={"kept": 1},
    )
    registry.set_meeting_status(meeting.id, "recorded")

    reached: list[int] = []
    original = Registry.interrupt_run

    def instrumented(self, run_id: int, **kwargs: object):
        reached.append(run_id)
        return original(self, run_id, **kwargs)

    monkeypatch.setattr(Registry, "interrupt_run", instrumented)

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        assert manager._reap_dead_runs([stale]) == []
        assert reached == [run.id]  # the guard was asked, and it said no
        kept = registry.get_run(run.id)
        assert kept.status == "done" and kept.error is None
        assert kept.progress == {"kept": 1}
        assert registry.meeting_by_id(meeting.id).status == "recorded"
    finally:
        manager.shutdown(timeout=5)


def test_a_reap_cannot_overwrite_a_run_its_owner_refreshed(tmp_path) -> None:
    """A beat written during reconciliation wins the reap (a stale observation).

    Reconciliation decides from a snapshot — a heartbeat that had gone stale —
    and a live owner beats every couple of seconds, so the row can go from
    "stale" to "beaten a moment ago" between the read that judged it dead and the
    write that would interrupt it. Interrupting it anyway would record a stop that
    never happened on a run whose owner is provably alive, so the transition is a
    compare-and-swap against the **ownership and heartbeat the decision was based
    on**: the beat wins, the run is left exactly as it was, and the reap reports
    the stale observation rather than an interruption it did not make.

    Staging the gap: the snapshot row is handed in directly, exactly as the
    reaper's read left it — the same stage the finished-run test above sets up for
    the ``status`` half of the compare, here for the heartbeat half.
    """
    from clear_record.core import read_recent

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(meeting.id, origin="console")
    # The owner is one the pid probe cannot resolve, so the **heartbeat** is what
    # decides here: an owner that looks like a live pid would hold the run on a
    # machine that happens to have that pid, and the test would be about the probe
    # instead of about the beat.
    unreadable = f"{platform.node()}:{'9' * 24}"
    assert registry.claim_run(run.id, owner=unreadable) is not None
    registry.set_meeting_status(meeting.id, "running")
    assert registry.heartbeat_run(run.id, at="2020-01-01T00:00:00+00:00")
    stale = registry.get_run(run.id)  # what the reaper judged dead from

    # The owner is alive and keeps beating: the row is no longer the stale one.
    assert registry.heartbeat_run(run.id)
    beaten = registry.get_run(run.id).heartbeat_at
    assert beaten != stale.heartbeat_at

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        assert manager._reap_dead_runs([stale]) == []  # a loss is not a reap
        kept = registry.get_run(run.id)
        assert (kept.status, kept.error) == ("running", None)
        assert kept.heartbeat_at == beaten
        assert registry.meeting_by_id(meeting.id).status == "running"
    finally:
        manager.shutdown(timeout=5)

    # And the loss is reported for what it was: a stale observation of a run that
    # is still running, i.e. an owner that refreshed its beat under the decision.
    reports = [
        json.loads(line) for line in read_recent(200) if "reconcile_stale" in line
    ]
    report = next(row for row in reports if row["run_id"] == run.id)
    assert report["status"] == "running"
    assert report["observed_heartbeat_at"] == "2020-01-01T00:00:00+00:00"
    assert report["heartbeat_at"] == beaten


def test_a_heartbeat_ahead_of_the_clock_is_not_evidence_of_life(tmp_path) -> None:
    """A backwards clock step must not pin a run — and the queue — in ``running``.

    Every writer reads this node's clock, so a beat *ahead* of now says the clock
    moved backwards after it was written, not that its owner is alive. Reading it
    as life would leave the orphan ``running`` until the clock caught up, and the
    claim's one-run guard would refuse every claim behind it in the meantime.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    busy = _meeting(registry, tmp_path, [tape])
    waiting = registry.create_meeting("ops", "Waiting", workspace_path=str(tmp_path))
    registry.set_recording_set(waiting.id, [str(tape)])

    peer_run = registry.create_run(
        busy.id,
        origin="mcp",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    # The owner is one the pid probe cannot resolve, so the **heartbeat** is
    # what decides here: an owner that looks like a live pid would hold the
    # run on a machine that happens to have that pid, and the test would be
    # about the probe instead of about the beat.
    assert registry.claim_run(peer_run.id, owner="peer:not-a-pid") is not None
    registry.set_meeting_status(busy.id, "running")
    # A beat written before the clock stepped backwards, i.e. dated after "now".
    assert registry.heartbeat_run(peer_run.id, at="2099-01-01T00:00:00+00:00")

    behind = registry.create_run(
        waiting.id,
        origin="console",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        assert manager.wait(behind.id, timeout=10).status == "done"
    finally:
        manager.shutdown(timeout=5)
    assert registry.get_run(peer_run.id).status == "interrupted"


def test_two_processes_claim_one_run_and_exactly_one_executes(tmp_path) -> None:
    """Two processes against one registry: exactly one executes (RUN-02).

    Both processes read the same queued run and both try to move it to
    ``running``; the conditional update picks one winner, and the run's ``owner``
    names it — so the loser is provably not a second executor. The run also ends
    clean: if either process had reconciled the other's live run away, the run
    would carry the restart reason on its record.
    """

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(
        meeting.id,
        origin="mcp",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )

    gate = tmp_path / "gate"
    executed = tmp_path / "executed.txt"
    children = []
    ready_files = []
    result_files = []
    for n in (0, 1):
        ready = tmp_path / f"ready-{n}"
        result = tmp_path / f"result-{n}.json"
        ready_files.append(ready)
        result_files.append(result)
        children.append(
            _run_child(
                tmp_path,
                _TWO_PROCESS_DRAIN,
                str(registry.db_path),
                str(gate),
                str(ready),
                str(result),
                str(executed),
                str(run.id),
            )
        )

    # Both processes are waiting to build their manager: release them together,
    # so the claim is raced rather than sequenced.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not all(p.exists() for p in ready_files):
        time.sleep(0.01)
    assert all(p.exists() for p in ready_files), "a child never reached the gate"
    gate.write_text("")

    for child in children:
        _, stderr = child.communicate(timeout=60)
        assert child.returncode == 0, stderr

    executors = executed.read_text().split()
    assert len(executors) == 1
    final = registry.get_run(run.id)
    assert final.status == "done"
    assert final.error is None  # neither process reaped the other's live run
    assert final.owner == f"{platform.node()}:{executors[0]}"

    reported = [json.loads(path.read_text()) for path in result_files]
    assert sum(1 for row in reported if row["executed"]) == 1
    assert {row["owner"] for row in reported} == {final.owner}


# --- the run cost record (RUN-01) ------------------------------------------ #

#: The pipeline stages a cost record times, in declared order.
_COST_STAGES = ("ingest", "align", "transcribe", "reconcile", "export")


def _fake_pipeline_with_transcript(directory, options, on_event) -> None:
    """The shape the real pipeline leaves behind, with no stage executed.

    One terminal event per stage (exactly what ``Progress`` emits) and the
    transcript meta the transcribe stage persists: source durations, the chunk
    report, the job count and the chunk size.
    """
    for stage in _COST_STAGES:
        step = Progress(stage, 1, on_event)
        step.start()
        step.advance(source=stage)
    Workspace.at(directory).write_segments(
        {"a": [], "b": []},
        meta={
            "backend": "fake",
            "model": "fake-model",
            "jobs": 3,
            "chunk_seconds": 30.0,
            "sources": {"a": {"duration": 120.0}, "b": {"duration": 60.0}},
            "chunk_report": {
                "scoped": False,
                "scope": "",
                "reused": 7,
                "redecoded": 2,
                "carried_over": 0,
                "guard_redecoded": 0,
            },
        },
    )


def test_a_completed_run_records_every_cost_primitive(tmp_path) -> None:
    """RUN-01: the run record holds the raw primitives, never a ratio.

    Wall-clock magnitudes are deliberately not asserted; the primitives that
    are not time are, and every stage must have its own entry.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    manager = RunManager(registry, pipeline=_fake_pipeline_with_transcript)
    run = manager.start(meeting, origin="console")
    assert manager.wait(run.id, timeout=10).status == "done"

    cost = registry.get_run(run.id).progress["cost"]
    assert set(cost["stages"]) == set(_COST_STAGES)
    assert all(seconds is not None for seconds in cost["stages"].values())
    assert cost["audio_seconds"] == 180.0
    assert cost["chunks"] == 9
    assert cost["chunks_reused"] == 7
    assert cost["chunks_redecoded"] == 2
    assert cost["backend"] == "fake"
    assert cost["model"] == "fake-model"
    assert cost["jobs"] == 3
    assert cost["chunk_seconds"] == 30.0
    assert cost["total_wall_seconds"] is not None
    assert cost["machine"]
    # A ratio is derived at display time; storing one would go stale.
    assert not {"speed", "realtime", "x_realtime", "ratio"} & set(cost)


def test_a_failed_run_records_the_stages_it_completed(tmp_path) -> None:
    """RUN-01: a run that stopped early still says how far it got."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def boom(directory, options, on_event) -> None:
        step = Progress("ingest", 1, on_event)
        step.start()
        step.advance(source="a")
        raise RuntimeError("backend unavailable")

    # A previous run left its transcript in the workspace. It is not this run's
    # measurement, so a run that failed before transcribe must not inherit it.
    Workspace.at(meeting.workspace_path).write_segments(
        {"a": []},
        meta={
            "backend": "fake",
            "model": "fake-model",
            "jobs": 3,
            "chunk_seconds": 30.0,
            "sources": {"a": {"duration": 120.0}},
            "chunk_report": {"reused": 7, "redecoded": 2},
        },
    )

    manager = RunManager(registry, pipeline=boom)
    run = manager.start(meeting, origin="console")
    assert manager.wait(run.id, timeout=10).status == "failed"

    cost = registry.get_run(run.id).progress["cost"]
    assert cost["stages"]["ingest"] is not None
    assert cost["stages"]["transcribe"] is None
    assert cost["audio_seconds"] is None
    assert cost["chunks"] is None
    assert cost["jobs"] is None
    assert cost["machine"]


def test_a_run_that_fails_after_transcribe_reports_its_transcript(tmp_path) -> None:
    """RUN-01: a failure after the transcribe write still measures the tape.

    Reconcile runs strictly after transcribe wrote ``segments.json``, so the
    transcript on disk is this run. The record must keep its real numbers even
    though the run failed. The *other* window — transcribe finished but the
    write never happened — is pinned by the sibling test below, not here: this
    fake emits all five terminal events before it raises.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def fake_pipeline(directory, options, on_event) -> None:
        _fake_pipeline_with_transcript(directory, options, on_event)
        raise RuntimeError("reconcile exploded")

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    assert manager.wait(run.id, timeout=10).status == "failed"

    cost = registry.get_run(run.id).progress["cost"]
    assert cost["audio_seconds"] == 180.0
    assert cost["chunks"] == 9
    assert cost["chunks_reused"] == 7
    assert cost["chunks_redecoded"] == 2
    assert cost["jobs"] == 3


def test_a_run_that_dies_before_writing_the_transcript_ignores_a_stale_one(
    tmp_path,
) -> None:
    """RUN-01: transcribe's terminal event precedes its ``segments.json`` write.

    A previous run left numbers in the workspace. This run emits every
    transcribe event (the last one is emitted by the chunk pool before the
    write) and then fails before writing its own transcript, so the stale
    numbers must not become this run record.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    Workspace.at(meeting.workspace_path).write_segments(
        {"a": []},
        meta={
            "backend": "fake",
            "model": "fake-model",
            "jobs": 8,
            "chunk_seconds": 30.0,
            "sources": {"a": {"duration": 9999.0}},
            "chunk_report": {"reused": 111, "redecoded": 222},
        },
    )

    def fake_pipeline(directory, options, on_event) -> None:
        for stage in ("ingest", "align", "transcribe"):
            step = Progress(stage, 1, on_event)
            step.start()
            step.advance(source=stage)
        raise RuntimeError("crashed before writing the transcript")

    manager = RunManager(registry, pipeline=fake_pipeline)
    run = manager.start(meeting, origin="console")
    assert manager.wait(run.id, timeout=10).status == "failed"

    cost = registry.get_run(run.id).progress["cost"]
    assert cost["stages"]["transcribe"] is not None  # this run did transcribe
    assert cost["audio_seconds"] is None
    assert cost["chunks"] is None
    assert cost["chunks_reused"] is None
    assert cost["jobs"] is None
    assert cost["backend"] == "apple"  # the row backend, not the stale meta


def test_an_interrupted_run_keeps_the_stages_it_completed(tmp_path) -> None:
    """Reconciliation records the cost of a run the process died in."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    orphan = registry.create_run(meeting.id, backend="apple")
    registry.update_run(
        orphan.id, status="running", started_at="2026-01-01T00:00:00+00:00"
    )
    registry.add_run_event(
        orphan.id,
        JobEvent(stage="ingest", index=1, total=1, done=True, elapsed_s=2.5),
    )

    manager = RunManager(registry, pipeline=lambda *args: None)
    try:
        cost = registry.get_run(orphan.id).progress["cost"]
        assert cost["stages"]["ingest"] == 2.5
        assert cost["stages"]["align"] is None
        assert cost["total_wall_seconds"] is not None
    finally:
        manager.shutdown()


def _completed_run(
    registry,
    meeting,
    *,
    backend: str,
    model: str,
    chunk_seconds: float,
    audio_seconds: float,
    wall_seconds: float,
):
    """Seed one completed run with the cost record the manager would write."""
    run = registry.create_run(
        meeting.id,
        backend=backend,
        model=model,
        run_options={"chunk_seconds": chunk_seconds},
    )
    registry.update_run(
        run.id,
        status="done",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:05:00+00:00",
        progress={
            "status": "done",
            "cost": {
                "audio_seconds": audio_seconds,
                "total_wall_seconds": wall_seconds,
                "chunk_seconds": chunk_seconds,
                "backend": backend,
                "model": model,
            },
        },
    )
    return run


def test_the_eta_projects_matching_history_onto_the_same_tape(tmp_path) -> None:
    """RUN-01: a second run of the same tape gets a history-based estimate.

    Seeded rows only and an explicit ``elapsed_s``, so the assertion never
    depends on how long anything actually took.
    """
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    _completed_run(
        registry,
        meeting,
        backend="apple",
        model="small",
        chunk_seconds=30.0,
        audio_seconds=600.0,
        wall_seconds=300.0,
    )
    # A newer run of the same tape with another model is three times faster on
    # paper; a mismatched model must not move the projection.
    _completed_run(
        registry,
        meeting,
        backend="apple",
        model="large",
        chunk_seconds=30.0,
        audio_seconds=600.0,
        wall_seconds=60.0,
    )
    run = registry.create_run(
        meeting.id,
        backend="apple",
        model="small",
        run_options={"chunk_seconds": 30.0},
    )

    # 600 audio seconds at 2 audio-seconds per wall second = 300s projected.
    assert estimate_eta_s(registry, run, elapsed_s=0.0) == 300.0
    assert estimate_eta_s(registry, run, elapsed_s=150.0) == 150.0


def test_the_eta_is_none_without_matching_history(tmp_path) -> None:
    """No match, a mismatched model, or a different chunk size: no estimate."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    run = registry.create_run(
        meeting.id,
        backend="apple",
        model="small",
        run_options={"chunk_seconds": 30.0},
    )
    # Nothing has completed yet: there is nothing to project.
    assert estimate_eta_s(registry, run, elapsed_s=0.0) is None

    # A completed run with another model is not history for this run.
    _completed_run(
        registry,
        meeting,
        backend="apple",
        model="large",
        chunk_seconds=30.0,
        audio_seconds=600.0,
        wall_seconds=60.0,
    )
    assert estimate_eta_s(registry, run, elapsed_s=0.0) is None

    # Nor is a matching run at another chunk size.
    _completed_run(
        registry,
        meeting,
        backend="apple",
        model="small",
        chunk_seconds=10.0,
        audio_seconds=600.0,
        wall_seconds=300.0,
    )
    assert estimate_eta_s(registry, run, elapsed_s=0.0) is None
