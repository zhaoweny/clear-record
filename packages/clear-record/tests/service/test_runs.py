"""The run manager: lifecycle, the live event stream, and artifact registration.

The pipeline callable is injected, so a full run — status transitions, progress
events, artifact checksums — is exercised with no ASR backend and no GPU.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from clear_record.core import Progress
from clear_record.service import Registry, RunManager


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(registry: Registry, tmp_path, tapes: list[Path]):
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    registry.set_recording_set(meeting.id, [str(tape) for tape in tapes])
    return meeting


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
