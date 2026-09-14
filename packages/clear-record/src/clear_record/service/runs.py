"""Background pipeline runs with a live progress stream.

A multi-hour tape must not block the web console, so a run executes on a worker
thread and its :class:`~clear_record.core.JobEvent` stream is retained for the
API/GUI (and for server-sent events) to read. The pipeline callable is
**injected**, so tests exercise the whole lifecycle — status transitions, the
event stream, artifact registration — with no ASR backend and no GPU.

The service drives the same stage wiring the CLI does
(``clear_record.cli.stages.run``), in-process: there is one pipeline
implementation, not two.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

from clear_record.cli import stages
from clear_record.core import EventSink, JobEvent
from clear_record.service.models import Meeting, PipelineRun
from clear_record.service.store import Registry

#: What the manager calls to run a pipeline: the CLI's stage wiring by default.
PipelineCallable = Callable[[str, "stages.PipelineOptions", EventSink | None], None]


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_artifacts(workspace: Path) -> list[tuple[str, Path]]:
    """The pipeline's outputs as ``(kind, path)`` pairs.

    Kinds line up with what the console shows: the reconciled ``record``, the
    ``transcript`` it came from, and each ``export`` file.
    """
    found: list[tuple[str, Path]] = []
    record = workspace / "record.json"
    if record.is_file():
        found.append(("record", record))
    segments = workspace / "segments.json"
    if segments.is_file():
        found.append(("transcript", segments))
    export_dir = workspace / "export"
    if export_dir.is_dir():
        found.extend(
            ("export", path) for path in sorted(export_dir.iterdir()) if path.is_file()
        )
    return found


def _default_pipeline(
    directory: str, options: "stages.PipelineOptions", on_event: EventSink | None
) -> None:
    stages.run(directory, options, on_event=on_event)


@dataclasses.dataclass
class RunState:
    """The live state of one run: its status and its event stream."""

    run_id: int
    meeting_id: int
    status: str = "queued"
    events: list[JobEvent] = dataclasses.field(default_factory=list)
    error: str | None = None

    def events_since(self, index: int) -> list[JobEvent]:
        """Events after ``index`` (the SSE cursor)."""
        return self.events[index:]

    @property
    def last(self) -> JobEvent | None:
        return self.events[-1] if self.events else None

    def summary(self) -> dict:
        last = self.last
        return {
            "run_id": self.run_id,
            "status": self.status,
            "events": len(self.events),
            "stage": last.stage if last else None,
            "index": last.index if last else 0,
            "total": last.total if last else 0,
            "eta_s": last.eta_s if last else None,
            "error": self.error,
        }


class RunManager:
    """Owns the background runs of one registry.

    One active run per meeting: starting a second while the first is in flight
    is refused, so a tape set is never transcribed twice concurrently.
    """

    def __init__(
        self, registry: Registry, pipeline: PipelineCallable | None = None
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline or _default_pipeline
        self._lock = threading.Lock()
        self._states: dict[int, RunState] = {}
        self._threads: dict[int, threading.Thread] = {}

    # --- reading ----------------------------------------------------------- #
    def state(self, run_id: int) -> RunState | None:
        with self._lock:
            return self._states.get(run_id)

    def require_state(self, run_id: int) -> RunState:
        state = self.state(run_id)
        if state is None:
            raise KeyError(run_id)
        return state

    def active_state(self, meeting_id: int) -> RunState | None:
        with self._lock:
            for state in self._states.values():
                if state.meeting_id == meeting_id and state.status in (
                    "queued",
                    "running",
                ):
                    return state
        return None

    # --- running ------------------------------------------------------------ #
    def start(
        self, meeting: Meeting, options: "stages.PipelineOptions | None" = None
    ) -> PipelineRun:
        if not meeting.workspace_path:
            raise ValueError("meeting has no workspace path; set one before running")
        tape_set = self._registry.latest_recording_set(meeting.id)
        if tape_set is None:
            raise ValueError("meeting has no tape set; select tapes before running")
        if self.active_state(meeting.id) is not None:
            raise ValueError("a run is already in flight for this meeting")

        options = dataclasses.replace(
            options or stages.PipelineOptions(), audio_files=tuple(tape_set.paths)
        )
        run = self._registry.create_run(
            meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
        )
        state = RunState(run_id=run.id, meeting_id=meeting.id, status="queued")
        with self._lock:
            self._states[run.id] = state
        thread = threading.Thread(
            target=self._execute,
            args=(meeting, options, run.id, state),
            name=f"cr-run-{run.id}",
            daemon=True,
        )
        self._threads[run.id] = thread
        thread.start()
        return run

    def _record(self, state: RunState, event: JobEvent) -> None:
        with self._lock:
            state.events.append(event)

    def _execute(
        self,
        meeting: Meeting,
        options: "stages.PipelineOptions",
        run_id: int,
        state: RunState,
    ) -> None:
        state.status = "running"
        self._registry.update_run(run_id, status="running", started_at=_now())
        self._registry.set_meeting_status(meeting.id, "running")

        def sink(event: JobEvent) -> None:
            self._record(state, event)

        try:
            self._pipeline(meeting.workspace_path, options, sink)
        except Exception as exc:  # noqa: BLE001 - recorded for the console, not hidden
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            self._registry.update_run(
                run_id,
                status="failed",
                ended_at=_now(),
                error=state.error,
                progress=state.summary(),
            )
            self._registry.set_meeting_status(meeting.id, "failed")
            return

        self._register_artifacts(meeting, run_id)
        state.status = "done"
        self._registry.update_run(
            run_id, status="done", ended_at=_now(), progress=state.summary()
        )
        self._registry.set_meeting_status(meeting.id, "recorded")

    def _register_artifacts(self, meeting: Meeting, run_id: int) -> None:
        assert meeting.workspace_path is not None
        for kind, path in collect_artifacts(Path(meeting.workspace_path)):
            try:
                digest = _sha256(path)
            except OSError:
                digest = None
            self._registry.add_artifact(
                meeting.id,
                run_id=run_id,
                kind=kind,
                path=str(path),
                sha256=digest,
                bytes=path.stat().st_size,
            )

    def wait(self, run_id: int, timeout: float | None = None) -> RunState:
        """Block until the run's thread finishes (tests; bounded by timeout)."""
        thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)
        return self.require_state(run_id)


__all__ = ["PipelineCallable", "RunManager", "RunState", "collect_artifacts"]
