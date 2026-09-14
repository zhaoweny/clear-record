"""Background pipeline runs with a durable progress stream and a node queue.

A multi-hour tape must not block the web console, so a run executes on a worker
thread and its :class:`~clear_record.core.JobEvent` stream is retained for the
API/GUI (and for server-sent events) to read. The pipeline callable is
**injected**, so tests exercise the whole lifecycle — status transitions, the
event stream, artifact registration — with no ASR backend and no GPU.

The service drives the same stage wiring the CLI does
(``clear_record.cli.stages.run``), in-process: there is one pipeline
implementation, not two.

Two properties make the node trustworthy across restarts:

* **The registry is the truth, not process memory.** A run's status and its
  event stream live in SQLite, so after the console restarts the run view still
  replays and the "one run per meeting" guard still holds. A run the process
  died in the middle of is reconciled to ``interrupted`` at startup (distinct
  from ``failed`` — the node died, the work did not necessarily fail).
* **One run per node.** :meth:`RunManager.start` **enqueues** a run (``queued``);
  a single scheduler drains the FIFO, so two meetings can no longer fight over
  one GPU. The queue is the registry's, so a restart does not lose queued work.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path

from clear_record.cli import stages
from clear_record.cli.workspace import Workspace
from clear_record.core import EventSink, JobEvent, PipelineOptions
from clear_record.core.diagnostics import log_event
from clear_record.core.i18n import deferred
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

#: A status that no later transition follows.
TERMINAL_STATUSES = ("done", "failed", "stopped", "interrupted")

#: The reason startup reconciliation records on a run left ``running`` by a dead
#: process. It is stored in the run's ``error`` column so the console (and a
#: diagnostics bundle) can show *why* the run is interrupted, not just that it is.
RESTART_REASON = "the console restarted while this run was in flight"


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
    """The state of one run: its status, its event stream and its queue place."""

    run_id: int
    meeting_id: int
    status: str = "queued"
    events: list[JobEvent] = dataclasses.field(default_factory=list)
    error: str | None = None
    #: 1-based FIFO position while ``queued`` (``1`` is next); ``0`` otherwise.
    position: int = 0

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
            "position": self.position,
        }


class RunManager:
    """Owns the background runs of one registry and the node's FIFO.

    One run **executing** per node (the queue), and no more than one
    **enqueued or running** run per meeting (the dedupe). Both are read from the
    registry, so a restart cannot break either invariant.
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
        #: Serializes event appends from a run's worker threads (a chunk pool
        #: reports from several at once), so the persisted stream has one order.
        self._events_lock = threading.Lock()
        #: Runs this process is executing, so startup reconciliation never
        #: interrupts a run that is alive (relevant when a second manager is
        #: opened over the same registry in-process).
        self._live: set[int] = set()
        #: Meeting + options for runs enqueued in this process, so a live run
        #: does not pay to re-read what it just wrote.
        self._pending: dict[int, tuple[Meeting, PipelineOptions]] = {}
        self._wake = threading.Condition()
        self._scheduler: threading.Thread | None = None
        self._stopping = False
        # A run the process died in the middle of is not running any more. Do
        # this before anything can drain the queue.
        self.reconcile()

    # --- startup reconciliation -------------------------------------------- #
    def reconcile(self) -> list[int]:
        """Move every ``running`` run this process did not start to ``interrupted``.

        Returns the reconciled run ids. The reason is recorded on the run (its
        ``error``) and logged; a meeting left ``running`` follows its run. Then
        the queue is drained, so **queued** work from a previous process is not
        lost by the restart.
        """
        interrupted: list[int] = []
        for run in self._registry.runs_with_status("running"):
            if run.id in self._live:
                continue
            self._registry.update_run(
                run.id, status="interrupted", ended_at=_now(), error=RESTART_REASON
            )
            meeting = self._registry.meeting_by_id(run.meeting_id)
            if meeting is not None and meeting.status == "running":
                self._registry.set_meeting_status(run.meeting_id, "interrupted")
            log_event(
                "warning",
                "runs",
                "run.interrupted",
                run_id=run.id,
                meeting_id=run.meeting_id,
                reason=RESTART_REASON,
            )
            interrupted.append(run.id)
        if interrupted:
            log_event(
                "warning",
                "runs",
                "run.reconciled",
                count=len(interrupted),
                run_ids=",".join(str(run_id) for run_id in interrupted),
            )
        self._ensure_scheduler()
        return interrupted

    # --- reading ----------------------------------------------------------- #
    def state(self, run_id: int) -> RunState | None:
        """The run's state read from the registry (events replay after a restart)."""
        row = self._registry.get_run(run_id)
        if row is None:
            return None
        return RunState(
            run_id=row.id,
            meeting_id=row.meeting_id,
            status=row.status,
            events=self._registry.list_run_events(run_id),
            error=row.error,
            position=(
                self._registry.queue_position(run_id) if row.status == "queued" else 0
            ),
        )

    def require_state(self, run_id: int) -> RunState:
        state = self.state(run_id)
        if state is None:
            raise KeyError(run_id)
        return state

    def active_state(self, meeting_id: int) -> RunState | None:
        """The meeting's live run, derived from the registry (guard, not memory)."""
        run = self._registry.active_run_for_meeting(meeting_id)
        return self.state(run.id) if run is not None else None

    # --- enqueuing ---------------------------------------------------------- #
    def start(
        self,
        meeting: Meeting,
        options: PipelineOptions | None = None,
        *,
        auto: dict | None = None,
    ) -> PipelineRun:
        """Enqueue a run at the back of the node's FIFO and return its row.

        The run is durable immediately (``queued``): if it cannot execute yet it
        keeps its place, and a restart still has it. The per-meeting dedupe is
        read from the registry, so the same tape set is never enqueued twice
        concurrently — but a *different* meeting now waits honestly instead of
        fighting for the one GPU.

        ``auto`` is the meta the opt-in ``--auto`` / ``--backend auto``
        resolvers produced (see :func:`clear_record.service.auto.resolve_run`):
        the explanations and which fields were chosen automatically. It is
        merged into the run meta, which always records the **resolved** profile
        and decoder knobs, so a finished run is explainable after the fact.
        """
        if not meeting.workspace_path:
            self._refuse(meeting, "meeting has no workspace path")
            raise ValueError(
                deferred("meeting has no workspace path; set one before running")
            )
        tape_set = self._registry.latest_recording_set(meeting.id)
        if tape_set is None:
            self._refuse(meeting, "meeting has no tape set")
            raise ValueError(
                deferred("meeting has no tape set; select tapes before running")
            )
        if self._registry.active_run_for_meeting(meeting.id) is not None:
            self._refuse(meeting, "a run is already in flight")
            raise ValueError(deferred("a run is already in flight for this meeting"))

        options = dataclasses.replace(
            options or PipelineOptions(), audio_files=tuple(tape_set.paths)
        )
        options, run_meta = self._resolve_glossary(meeting, options)
        run = self._registry.create_run(
            meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
            options=self._run_meta(options, run_meta, auto),
            run_options=dataclasses.asdict(options),
        )
        with self._lock:
            self._pending[run.id] = (meeting, options)
        log_event(
            "info",
            "runs",
            "run.enqueued",
            run_id=run.id,
            meeting_id=meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
        )
        self._ensure_scheduler()
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

    @staticmethod
    def _run_meta(
        options: PipelineOptions, glossary_meta: dict, auto: dict | None
    ) -> dict:
        """The run meta recorded at start: the **resolved** profile and knobs,
        the glossary identity, and — when the opt-in resolvers ran — how they
        chose.

        Every value here is post-precedence: the same resolution the run
        executes with, never the requested values. ``decoder_knobs`` keeps only
        the knobs that are actually set, so an unset knob cannot masquerade as a
        choice. ``auto`` carries the CLI's own explanation and the fields it
        supplied (see :func:`clear_record.service.auto.resolve_run`).
        """
        meta: dict = {
            "profile": options.profile,
            "decoder_knobs": options.decoder_knobs(),
        }
        if auto:
            meta.update(auto)
        meta.update(glossary_meta)
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

    # --- the scheduler ------------------------------------------------------ #
    def _ensure_scheduler(self) -> None:
        """Start (or wake) the one thread that drains the queue."""
        with self._wake:
            self._stopping = False
            if self._scheduler is not None and self._scheduler.is_alive():
                self._wake.notify_all()
                return
            self._scheduler = threading.Thread(
                target=self._drain, name="cr-run-queue", daemon=True
            )
            self._scheduler.start()

    def _drain(self) -> None:
        """Run queued runs one at a time, FIFO, until asked to stop.

        The wait has a timeout so a run enqueued *outside* this manager (another
        writer of the same registry) is still noticed; the normal path wakes the
        thread immediately.
        """
        while True:
            with self._wake:
                while True:
                    if self._stopping:
                        return
                    if not self._registry.runs_with_status("running"):
                        run = self._registry.oldest_queued_run()
                        if run is not None:
                            break
                    self._wake.wait(timeout=1.0)
            self._execute_run(run)

    def _execute_run(self, run: PipelineRun) -> None:
        with self._lock:
            pending = self._pending.pop(run.id, None)
            self._live.add(run.id)
        try:
            meeting = (
                pending[0]
                if pending is not None
                else self._registry.meeting_by_id(run.meeting_id)
            )
            options = pending[1] if pending is not None else self._options_from_row(run)
            tape_set = self._registry.latest_recording_set(run.meeting_id)
            if meeting is None or options is None or tape_set is None:
                self._fail_unrunnable(
                    run,
                    meeting,
                    "the queued run has no meeting, options or tape set",
                )
                return
            # The tape set is re-read at execution time, not at enqueue time.
            options = dataclasses.replace(options, audio_files=tuple(tape_set.paths))
            self._run_pipeline(run, meeting, options)
        except Exception as exc:  # noqa: BLE001 - a run must not strand; the queue lives on
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._registry.update_run(
                    run.id,
                    status="failed",
                    ended_at=_now(),
                    error=error,
                    progress=self._progress(run.id, "failed", error),
                )
            except Exception:  # noqa: BLE001 - the registry is the last resort
                pass
            log_event(
                "error",
                "runs",
                "run.failed",
                run_id=run.id,
                meeting_id=run.meeting_id,
                error=error,
            )
        finally:
            with self._lock:
                self._live.discard(run.id)

    def _options_from_row(self, run: PipelineRun) -> PipelineOptions | None:
        """Rebuild the queued run's options from the registry (after a restart)."""
        if not run.run_options:
            return None
        known = {field.name for field in dataclasses.fields(PipelineOptions)}
        values = {key: value for key, value in run.run_options.items() if key in known}
        for name in ("audio_files", "formats"):
            if values.get(name) is not None:
                values[name] = tuple(values[name])
        return PipelineOptions(**values)

    def _run_pipeline(
        self, run: PipelineRun, meeting: Meeting, options: PipelineOptions
    ) -> None:
        assert meeting.workspace_path is not None
        self._registry.update_run(run.id, status="running", started_at=_now())
        self._registry.set_meeting_status(meeting.id, "running")
        log_event(
            "info",
            "runs",
            "run.started",
            run_id=run.id,
            meeting_id=meeting.id,
            backend=options.backend,
            model=options.model,
            language=options.language,
        )
        self._webhooks.emit(
            RUN_STARTED,
            project_id=meeting.project_id,
            meeting_id=meeting.id,
            run_id=run.id,
        )

        def sink(event: JobEvent) -> None:
            with self._events_lock:
                self._registry.add_run_event(run.id, event)

        try:
            self._pipeline(meeting.workspace_path, options, sink)
        except Exception as exc:  # noqa: BLE001 - recorded for the console, not hidden
            error = f"{type(exc).__name__}: {exc}"
            self._registry.update_run(
                run.id,
                status="failed",
                ended_at=_now(),
                error=error,
                progress=self._progress(run.id, "failed", error),
            )
            self._registry.set_meeting_status(meeting.id, "failed")
            log_event(
                "error",
                "runs",
                "run.failed",
                run_id=run.id,
                meeting_id=meeting.id,
                error=error,
            )
            self._webhooks.emit(
                RUN_FAILED,
                project_id=meeting.project_id,
                meeting_id=meeting.id,
                run_id=run.id,
            )
            return

        artifacts = self._register_artifacts(meeting, run.id)
        self._registry.update_run(
            run.id,
            status="done",
            ended_at=_now(),
            progress=self._progress(run.id, "done"),
        )
        self._registry.set_meeting_status(meeting.id, "recorded")
        log_event(
            "info",
            "runs",
            "run.finished",
            run_id=run.id,
            meeting_id=meeting.id,
            artifacts=len(artifacts),
        )
        self._webhooks.emit(
            RUN_FINISHED,
            project_id=meeting.project_id,
            meeting_id=meeting.id,
            run_id=run.id,
        )
        if any(kind == "transcript" for kind, _ in artifacts):
            self._webhooks.emit(
                TRANSCRIPT_READY,
                project_id=meeting.project_id,
                meeting_id=meeting.id,
                run_id=run.id,
            )

    def _fail_unrunnable(
        self, run: PipelineRun, meeting: Meeting | None, error: str
    ) -> None:
        """Fail a queued run that cannot execute at all (e.g. its tape set is gone)."""
        self._registry.update_run(
            run.id,
            status="failed",
            ended_at=_now(),
            error=error,
            progress=self._progress(run.id, "failed", error),
        )
        if meeting is not None:
            self._registry.set_meeting_status(meeting.id, "failed")
            self._webhooks.emit(
                RUN_FAILED,
                project_id=meeting.project_id,
                meeting_id=meeting.id,
                run_id=run.id,
            )
        log_event(
            "error",
            "runs",
            "run.failed",
            run_id=run.id,
            meeting_id=run.meeting_id,
            error=error,
        )

    def _progress(self, run_id: int, status: str, error: str | None = None) -> dict:
        """The recorded progress summary for a terminal transition."""
        events = self._registry.list_run_events(run_id)
        last = events[-1] if events else None
        return {
            "run_id": run_id,
            "status": status,
            "events": len(events),
            "stage": last.stage if last else None,
            "index": last.index if last else 0,
            "total": last.total if last else 0,
            "eta_s": last.eta_s if last else None,
            "error": error,
        }

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

    # --- lifecycle ---------------------------------------------------------- #
    def wait(self, run_id: int, timeout: float | None = None) -> RunState:
        """Block until the run reaches a terminal status (tests; bounded by timeout)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = self.require_state(run_id)
            if state.status in TERMINAL_STATUSES:
                return state
            if deadline is not None and time.monotonic() >= deadline:
                return state
            time.sleep(0.01)

    def shutdown(self, timeout: float | None = 0.0) -> None:
        """Ask the queue to stop draining (a clean console shutdown).

        Best-effort and **bounded**: an executing run has no cancellation
        contract, so this signals the scheduler and returns without waiting for
        a multi-hour pipeline. Pass a real ``timeout`` to wait for the scheduler
        thread itself (it exits promptly when no run is executing).
        """
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
            scheduler = self._scheduler
        if scheduler is not None:
            scheduler.join(timeout)
        with self._wake:
            if self._scheduler is scheduler and not scheduler.is_alive():
                self._scheduler = None


__all__ = [
    "PipelineCallable",
    "PipelineOptions",
    "RESTART_REASON",
    "RunManager",
    "RunState",
    "TERMINAL_STATUSES",
    "collect_artifacts",
]
