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
from clear_record.cli.workspace import Workspace
from clear_record.core import EventSink, JobEvent, PipelineOptions
from clear_record.core.diagnostics import log_event
from clear_record.service.glossary import (
    project_snapshot,
    snapshot_from_text,
    write_snapshot,
)
from clear_record.service.models import Meeting, PipelineRun
from clear_record.service.store import Registry
from clear_record.service.webhooks import (
    RUN_FAILED,
    RUN_FINISHED,
    RUN_STARTED,
    TRANSCRIPT_READY,
    WebhookEmitter,
    default_emitter,
)

#: What the manager calls to run a pipeline: the CLI's stage wiring by default.
PipelineCallable = Callable[[str, PipelineOptions, EventSink | None], None]

#: The pipeline run configuration. Owned by ``clear_record.core`` (dependency-free)
#: and re-exported here for the service's callers (web, MCP, scripts), so they can
#: set options without importing the CLI's stage module themselves. It is the same
#: object as ``clear_record.cli.stages.PipelineOptions``.


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
    directory: str, options: PipelineOptions, on_event: EventSink | None
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
        self,
        registry: Registry,
        pipeline: PipelineCallable | None = None,
        webhooks: WebhookEmitter | None = None,
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline or _default_pipeline
        # Delivery is opt-in (no configured endpoints = inert) and off-thread, so
        # a webhook can never fail or stall a run.
        self._webhooks = webhooks if webhooks is not None else default_emitter()
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
        self, meeting: Meeting, options: PipelineOptions | None = None
    ) -> PipelineRun:
        if not meeting.workspace_path:
            self._refuse(meeting, "meeting has no workspace path")
            raise ValueError("meeting has no workspace path; set one before running")
        tape_set = self._registry.latest_recording_set(meeting.id)
        if tape_set is None:
            self._refuse(meeting, "meeting has no tape set")
            raise ValueError("meeting has no tape set; select tapes before running")
        if self.active_state(meeting.id) is not None:
            self._refuse(meeting, "a run is already in flight")
            raise ValueError("a run is already in flight for this meeting")

        options = dataclasses.replace(
            options or PipelineOptions(), audio_files=tuple(tape_set.paths)
        )
        options, run_meta = self._resolve_glossary(meeting, options)
        run = self._registry.create_run(
            meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
            options=run_meta,
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

    @staticmethod
    def _glossary_meta(path: Path) -> dict:
        """The run meta for a glossary **file**: its path and, if readable, its
        canonical sha256 (so an explicit file's identity is comparable to a
        project snapshot's)."""
        meta: dict = {"glossary": str(path)}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return meta
        meta["glossary_sha256"] = snapshot_from_text(text).sha256
        return meta

    def _resolve_glossary(
        self, meeting: Meeting, options: PipelineOptions
    ) -> tuple[PipelineOptions, dict]:
        """Apply the glossary a run should use and return its recorded identity.

        Precedence, strongest first:

        1. An **explicit** glossary always wins; its terms are hashed so the run
           meta is comparable to a snapshot's.
        2. Otherwise the **project's confirmed-term snapshot** wins **when it has
           terms**: it is written to the meeting's workspace ``glossary.txt``, so
           a glossary edit reaches the next run with no extra wiring (the
           ADR-0018 tuning loop).
        3. When the project has **no confirmed terms**, the registry has nothing
           to say and the user's ``glossary.txt`` — a documented, user-editable
           artifact (``clear-record glossary`` / the README) — **stands**: it is
           left untouched and used as the run's glossary. With no such file the
           run simply has no glossary.

        Candidates and retired terms never reach the decoder. The returned meta
        records the glossary path and its sha256, so a re-run is explainable.
        """
        if options.glossary is not None:
            return options, self._glossary_meta(Path(options.glossary))

        assert meeting.workspace_path is not None  # guaranteed by start()
        workspace = Workspace.at(meeting.workspace_path)
        snapshot = project_snapshot(self._registry, meeting.project_slug)
        if snapshot.empty:
            # The registry is authoritative only when it has confirmed terms;
            # otherwise the user's file stands and is never clobbered.
            if workspace.glossary_path.exists():
                return options, self._glossary_meta(workspace.glossary_path)
            return options, {}

        path = write_snapshot(workspace, snapshot)
        return (
            dataclasses.replace(options, glossary=str(path)),
            {"glossary": str(path), "glossary_sha256": snapshot.sha256},
        )

    def _refuse(self, meeting: Meeting, reason: str) -> None:
        """Log a start refusal (an expected user error, not a crash)."""
        log_event(
            "warning",
            "runs",
            "run.refused",
            meeting_id=meeting.id,
            reason=reason,
        )

    def _record(self, state: RunState, event: JobEvent) -> None:
        with self._lock:
            state.events.append(event)

    def _execute(
        self,
        meeting: Meeting,
        options: PipelineOptions,
        run_id: int,
        state: RunState,
    ) -> None:
        state.status = "running"
        self._registry.update_run(run_id, status="running", started_at=_now())
        self._registry.set_meeting_status(meeting.id, "running")
        log_event(
            "info",
            "runs",
            "run.started",
            run_id=run_id,
            meeting_id=meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
        )
        self._webhooks.emit(
            RUN_STARTED,
            project_id=meeting.project_id,
            meeting_id=meeting.id,
            run_id=run_id,
        )

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
            log_event(
                "error",
                "runs",
                "run.failed",
                run_id=run_id,
                meeting_id=meeting.id,
                error=state.error,
            )
            self._webhooks.emit(
                RUN_FAILED,
                project_id=meeting.project_id,
                meeting_id=meeting.id,
                run_id=run_id,
            )
            return

        artifacts = self._register_artifacts(meeting, run_id)
        state.status = "done"
        self._registry.update_run(
            run_id, status="done", ended_at=_now(), progress=state.summary()
        )
        self._registry.set_meeting_status(meeting.id, "recorded")
        log_event(
            "info",
            "runs",
            "run.finished",
            run_id=run_id,
            meeting_id=meeting.id,
            artifacts=len(artifacts),
        )
        self._webhooks.emit(
            RUN_FINISHED,
            project_id=meeting.project_id,
            meeting_id=meeting.id,
            run_id=run_id,
        )
        if any(kind == "transcript" for kind, _ in artifacts):
            self._webhooks.emit(
                TRANSCRIPT_READY,
                project_id=meeting.project_id,
                meeting_id=meeting.id,
                run_id=run_id,
            )

    def _register_artifacts(
        self, meeting: Meeting, run_id: int
    ) -> list[tuple[str, Path]]:
        assert meeting.workspace_path is not None
        artifacts = collect_artifacts(Path(meeting.workspace_path))
        for kind, path in artifacts:
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
        return artifacts

    def wait(self, run_id: int, timeout: float | None = None) -> RunState:
        """Block until the run's thread finishes (tests; bounded by timeout)."""
        thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)
        return self.require_state(run_id)


__all__ = [
    "PipelineCallable",
    "PipelineOptions",
    "RunManager",
    "RunState",
    "collect_artifacts",
]
