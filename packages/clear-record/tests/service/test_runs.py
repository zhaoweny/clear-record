"""The run manager: lifecycle, the live event stream, and artifact registration.

The pipeline callable is injected, so a full run — status transitions, progress
events, artifact checksums — is exercised with no ASR backend and no GPU.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

from clear_record.cli.workspace import Workspace
from clear_record.core import Progress, resolve_options
from clear_record.service import (
    PipelineOptions,
    Registry,
    RunManager,
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
    run = manager.start(meeting)
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
    first = manager.start(meeting)
    manager.wait(first.id, timeout=10)

    registry.add_term("ops", "Booster", status="confirmed")
    second = manager.start(meeting)
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
    run = manager.start(meeting, PipelineOptions(glossary=str(explicit)))
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
    run = manager.start(meeting)
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
    run = manager.start(meeting)
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
    run = manager.start(meeting)
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
    run = manager.start(meeting)
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
    state = manager.wait(manager.start(meeting).id, timeout=10)

    assert state.status == "failed"
    assert "backend unavailable" in (state.error or "")
    assert registry.get_run(state.run_id).status == "failed"
    assert registry.meeting_by_id(meeting.id).status == "failed"
    assert registry.list_artifacts(meeting.id) == []


def test_run_requires_a_workspace_and_a_tape_set(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    manager = RunManager(registry, pipeline=lambda *args: None)

    no_tapes = registry.create_meeting("ops", "No tapes", workspace_path=str(tmp_path))
    with pytest.raises(ValueError):
        manager.start(no_tapes)

    no_workspace = registry.create_meeting("ops", "No workspace")
    registry.set_recording_set(no_workspace.id, [str(tmp_path / "x.wav")])
    with pytest.raises(ValueError):
        manager.start(no_workspace)


def test_a_second_run_is_refused_while_one_is_in_flight(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    release = threading.Event()

    def slow_pipeline(directory, options, on_event) -> None:
        release.wait(10)

    manager = RunManager(registry, pipeline=slow_pipeline)
    run = manager.start(meeting)
    with pytest.raises(ValueError):
        manager.start(meeting)

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
    state = manager.wait(manager.start(meeting).id, timeout=10)

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
    run = manager.start(meeting)
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
    fresh = manager.start(meeting)
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
    runs = [manager.start(meeting) for meeting in meetings]

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
    run = manager.start(meeting)
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
        manager.start(meeting)

    release.set()
    assert manager.wait(fresh.id, timeout=10).status == "done"
