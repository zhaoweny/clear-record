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
  replays and the "one run per meeting" guard still holds. The per-meeting half
  of that is enforced by the registry itself — revision 0009's partial unique
  index over the meeting's active run — so the second of two submissions that
  race past the guard is refused by the database, not by the check that read
  before the other write. A run whose owner died is reconciled to
  ``interrupted`` (distinct from ``failed`` — the node died, the work did not
  necessarily fail).
* **One run per node, one claim.** :meth:`RunManager.start` **enqueues** a run
  (``queued``); a scheduler drains the FIFO, and the move to ``running`` is a
  single conditional update in the registry, so the console, an agent's MCP
  server share one queue and exactly one of them executes a run. The queue is
  the registry's, so a restart does not lose queued work.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import platform as _platform
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from clear_record.cli import stages
from clear_record.cli.workspace import Workspace
from clear_record.core import EventSink, JobEvent, PipelineOptions, RunCancelled
from clear_record.core.diagnostics import log_event
from clear_record.core.i18n import deferred, tr
from clear_record.core.pipeline import pipeline_spec
from clear_record.service.diagnostics import machine_description
from clear_record.service.glossary import (
    project_snapshot,
    snapshot_from_text,
    write_snapshot,
)
from clear_record.service.models import (
    ACTIVE_RUN_STATUSES,
    RUN_ORIGINS,
    TERMINAL_STATUSES,
    Meeting,
    PipelineRun,
)
from clear_record.service.run_options import MalformedRunOptions
from clear_record.service.schemas import Shape
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
#: The ``on_event`` it receives is a :class:`RunChannel` — the event sink plus the
#: run's cancel signal (RUN-04) — so a pipeline's cancellation needs no extra
#: parameter to thread.
PipelineCallable = Callable[[str, PipelineOptions, EventSink | None], None]


class RunChannel:
    """What the run queue hands one pipeline: the event sink, plus its stop button.

    One object per run, and the only thing the manager passes a pipeline that the
    manager owns. Reporting goes through it (``channel(event)``), and the run's
    cancel signal travels with it:

    * a **set signal makes the next report raise** :class:`RunCancelled`, so any
      pipeline that reports progress stops at its next event — a stage's first
      announcement is a stage boundary, and transcribe reports per chunk;
    * :attr:`signal` is the same signal, for the one stage that owns something
      long-running: transcribe's chunk pool reads it so a cancel terminates its
      decoder children promptly instead of waiting for a chunk to finish.

    Nothing is persisted for the report that stopped the run: the stream ends at
    the last thing the work actually said.
    """

    def __init__(
        self,
        registry: Registry,
        run_id: int,
        events_lock: threading.Lock,
        signal: threading.Event,
    ) -> None:
        self._registry = registry
        self._run_id = run_id
        self._events_lock = events_lock
        self._signal = signal

    @property
    def signal(self) -> threading.Event:
        """The run's cancel signal (see this class's docstring)."""
        return self._signal

    def __call__(self, event: JobEvent) -> None:
        if self._signal.is_set():
            raise RunCancelled("the run was cancelled")
        with self._events_lock:
            self._registry.add_run_event(self._run_id, event)


#: The reason startup reconciliation records on a run left ``running`` by a dead
#: process. It is stored in the run's ``error`` column so the console (and a
#: diagnostics bundle) can show *why* the run is interrupted, not just that it is.
#:
#: The console renders that column verbatim (``web/templates/_run.html``,
#: ``_activity_run.html``), which ``docs/i18n.md`` puts on the translated side of
#: the boundary, so the value written **to the row** is looked up with ``tr`` at
#: that moment (see :meth:`_reap_dead_runs`). The same message ID stays English
#: where it is machine-facing: the JSONL log record and the run's ``progress``
#: summary both carry it unrendered. ``deferred`` is the extraction marker.
RESTART_REASON = deferred("the console restarted while this run was in flight")

#: The sentence a second submission for one meeting is refused with — the one
#: message the guard and the database's index both end at, raised by
#: :meth:`RunManager._active_run_refusal` and answered verbatim by the JSON API's
#: own pre-check (``web.app.start_run``), so the same condition cannot come to
#: read two ways. A message ID, looked up where it is shown: the console renders
#: it with ``tr``, the JSON API and the MCP tool carry the ID (both are
#: machine-facing surfaces, ``docs/i18n.md``).
RUN_IN_FLIGHT = deferred("a run is already in flight for this meeting")

#: The pipeline stages, in declared order: a run's cost record times each one
#: (the spec's "render" is the pipeline's final ``export`` stage).
_STAGES: tuple[str, ...] = tuple(step.value for step in pipeline_spec().steps)

#: How many of the newest completed runs a history-based ETA draws on.
_ETA_HISTORY_LIMIT = 50

#: How often an executing manager refreshes its run's liveness heartbeat, and how
#: old a heartbeat may be before another process reads the run as left by a dead
#: owner (RUN-02). The interval is short against the deadline, so a loaded machine
#: — one whose pipeline is holding the GIL — still beats many times inside it.
#: The deadline governs only runs whose owner this node cannot probe (see
#: :func:`_local_owner_state`); a *local* owner is decided by the process itself.
HEARTBEAT_INTERVAL_S = 2.0
HEARTBEAT_STALE_S = 30.0

#: This node's name as it appears in a run's ``owner`` — one source of truth for
#: the identity written at claim time and the one read back to probe the process.
_HOST = _platform.node() or "unknown"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _process_identity() -> str:
    """This process's claim identity: ``host:pid``, written on every claim.

    It is an identity, not a fingerprint: enough to say *which* process owns a
    run in the status data, to probe whether that process still exists on this
    host, and to grow a node column from later.
    """
    return f"{_HOST}:{os.getpid()}"


def _local_owner_state(owner: str | None) -> bool | None:
    """Whether a run's owner is a live process **here**, or provably gone from here.

    ``True`` a live process on this host owns the run, ``False`` the owner's
    process is gone from this host, ``None`` this node cannot tell — the caller
    then falls back to the heartbeat, which is evidence about a process rather
    than proof of one.

    The pid is probed first and its answer is read **asymmetrically**, because the
    two directions do not carry the same risk:

    * **A live pid holds the run, whatever the recorded host text says.** A
      process that exists is the strongest evidence there is, and the host text
      is a snapshot of a *mutable* name: ``platform.node()`` follows the machine's
      network name, so a VPN switch, a rename or a container can change it under
      a running registry. Reading that mismatch as "not ours" would free the node
      and admit a second pipeline beside a stalled-but-alive owner — the one
      thing this rule exists to prevent.
    * **A gone pid frees the run only when the owner named *this* host**, because
      "no such pid here" says nothing about a process on another host. A renamed
      host therefore falls back to the heartbeat and reaps a killed owner's run a
      deadline later rather than at once: slower, never wrong in the direction
      that matters.

    The probe is ``kill(pid, 0)`` — existence, not signalling — and it is only
    attempted on POSIX: on Windows that call *terminates* the process, so a
    registry shared with one gets the heartbeat's answer instead.
    """
    if not owner:
        return None
    host, sep, pid_text = owner.rpartition(":")
    if not sep or os.name != "posix":
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    if pid <= 0:  # a pid group (or a corrupt string), not a process
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False if host == _HOST else None
    except PermissionError:
        return True  # exists, owned by someone else on this host
    except OverflowError:
        # A pid outside C's int range: an unparseable owner, not a process. It
        # must answer like any other unparseable owner rather than raise — this
        # runs from reconciliation (a manager's constructor) and from the drain
        # loop, where an escaping exception would stop the console from starting
        # and, once running, would kill the queue thread.
        return None
    except OSError:
        return None  # cannot tell: let the heartbeat decide
    return True


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


def number_or_none(value: object) -> float | None:
    """A JSON number as a float (``None`` for anything else, bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def int_or_none(value: object) -> int | None:
    """A JSON integer (``None`` for anything else, bools included)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_time(value: str | None) -> _dt.datetime | None:
    """An ISO-8601 registry timestamp as an aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _wall_seconds(started_at: str | None, ended_at: str | None) -> float | None:
    """Wall seconds between two registry timestamps (``None`` when unknown)."""
    started = _parse_time(started_at)
    ended = _parse_time(ended_at)
    if started is None or ended is None:
        return None
    seconds = (ended - started).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def _elapsed_since(started_at: str | None) -> float:
    """Wall seconds since a run started (0.0 while it has not started yet)."""
    started = _parse_time(started_at)
    if started is None:
        return 0.0
    return max(0.0, (_dt.datetime.now(_dt.UTC) - started).total_seconds())


def _audio_seconds(meta: dict) -> float | None:
    """Audio seconds processed, summed from the durations transcribe recorded."""
    sources = meta.get("sources")
    if not isinstance(sources, dict):
        return None
    total = 0.0
    known = False
    for source in sources.values():
        duration = (
            number_or_none(source.get("duration")) if isinstance(source, dict) else None
        )
        if duration is not None and duration > 0:
            total += duration
            known = True
    return round(total, 3) if known else None


def cost_of(run: PipelineRun) -> dict:
    """The cost record a run persisted (``{}`` when it has none)."""
    progress = run.progress if isinstance(run.progress, dict) else None
    cost = progress.get("cost") if progress else None
    return cost if isinstance(cost, dict) else {}


def _options_chunk_seconds(run: PipelineRun) -> float | None:
    """The chunk size the run resolved (its queued options), if recorded."""
    options = run.run_options if isinstance(run.run_options, dict) else {}
    return number_or_none(options.get("chunk_seconds"))


def _run_chunk_seconds(run: PipelineRun) -> float | None:
    """The chunk size a run executes with: its cost record, else its options."""
    recorded = number_or_none(cost_of(run).get("chunk_seconds"))
    return recorded if recorded is not None else _options_chunk_seconds(run)


def _same_seconds(left: float | None, right: float | None) -> bool:
    """Two durations equal within the rounding a JSON round-trip keeps."""
    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) <= 1e-6


def _tape_seconds(registry: Registry, run: PipelineRun) -> float | None:
    """The meeting's newest recorded audio seconds (the tape it re-runs)."""
    for candidate in registry.list_runs(run.meeting_id):
        if candidate.id == run.id:
            continue
        value = number_or_none(cost_of(candidate).get("audio_seconds"))
        if value is not None and value > 0:
            return value
    return None


def estimate_eta_s(
    registry: Registry,
    run: PipelineRun,
    *,
    audio_seconds: float | None = None,
    elapsed_s: float | None = None,
) -> float | None:
    """History-based seconds remaining for a queued or running run (RUN-01).

    The projection is **derived here, at display time**, from raw primitives:
    completed runs whose ``(backend, model, chunk_seconds)`` match this run's
    contribute their audio seconds and total wall seconds, which give the
    audio-seconds-per-wall-second this machine sustains for this configuration.
    The run's own tape duration is projected through that rate; the caller may
    supply it, otherwise it comes from the meeting's newest record (the re-run
    case: the tape a run re-runs is the meeting's).

    ``None`` means "no estimate" — no matching history, no known tape duration,
    or a run that is already terminal. The caller keeps its live stage-local
    estimate in that case; this function never invents one.
    """
    if run.status not in ACTIVE_RUN_STATUSES:
        return None
    chunk_seconds = _run_chunk_seconds(run)
    history_audio = 0.0
    history_wall = 0.0
    for candidate in registry.runs_with_status("done", limit=_ETA_HISTORY_LIMIT):
        if candidate.id == run.id:
            continue
        if candidate.backend != run.backend or candidate.model != run.model:
            continue
        if not _same_seconds(_run_chunk_seconds(candidate), chunk_seconds):
            continue
        cost = cost_of(candidate)
        candidate_audio = number_or_none(cost.get("audio_seconds"))
        candidate_wall = number_or_none(cost.get("total_wall_seconds"))
        if candidate_audio is None or candidate_wall is None:
            continue
        if candidate_audio <= 0 or candidate_wall <= 0:
            continue
        history_audio += candidate_audio
        history_wall += candidate_wall
    if history_audio <= 0 or history_wall <= 0:
        return None
    if audio_seconds is None:
        audio_seconds = _tape_seconds(registry, run)
    total_audio = number_or_none(audio_seconds)
    if total_audio is None or total_audio <= 0:
        return None
    if elapsed_s is None:
        elapsed_s = _elapsed_since(run.started_at)
    projected = total_audio / (history_audio / history_wall)
    return round(max(0.0, projected - elapsed_s), 3)


def _default_pipeline(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    stages.run(directory, options, on_event=on_event)


class RunSummary(Shape):
    """One run's live state as a reader sees it (ADR-0030).

    The console's run fragment, the JSON API and the MCP tools all report a run's
    progress through this, so a browser and an agent cannot be told different
    things about the same run. ``stage``/``index``/``total``/``eta_s`` come from
    the newest event and are ``None``/``0`` while the run has reported nothing.
    """

    run_id: int
    status: str
    events: int
    stage: str | None
    index: int
    total: int
    eta_s: float | None
    error: str | None
    position: int


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

    def summary(self) -> RunSummary:
        last = self.last
        return RunSummary(
            run_id=self.run_id,
            status=self.status,
            events=len(self.events),
            stage=last.stage if last else None,
            index=last.index if last else 0,
            total=last.total if last else 0,
            eta_s=last.eta_s if last else None,
            error=self.error,
            position=self.position,
        )


class RunManager:
    """Owns the background runs of one registry and the node's FIFO.

    One run **executing** per node (the queue), and no more than one
    **enqueued or running** run per meeting (the dedupe). The queue's rule is
    the registry's: a queued run is claimed by one conditional update, so a
    restart loses no queued work and no two processes execute one run. The
    per-meeting rule is the **table's** — revision 0009's partial unique index
    over the meeting's active run — and the read in
    :meth:`~clear_record.service.store.Registry.active_run_for_meeting` is the
    guard that refuses the common case without writing a row; when two
    submissions race past that read, the database refuses the second.

    The queue is **cross-process** (RUN-02): a queued run is claimed by one
    conditional update (:meth:`~clear_record.service.store.Registry.claim_run`),
    so the console and an agent's MCP server can both write to the same registry
    and exactly one of them executes a run — the CLI is not a third writer: it
    runs the pipeline in-process (``cli.cli._cmd_run``) and writes no run row, so
    the ``cli`` origin is recorded only for a run a CLI-shaped surface enqueues
    through this service. The in-process pieces are a fast path, not the
    guarantee: :attr:`_pending` saves re-reading what this process just wrote,
    and :attr:`_live` says what this process is executing.

    An executing manager **beats** (:attr:`_heartbeat`), refreshing its run's
    liveness heartbeat, and reconciliation reads that beat together with the
    owner's recorded ``host:pid``: a second manager — another process, or another
    manager in this process — leaves a live owner's run alone, a killed owner's
    run is reconciled at once, and an owner this node cannot see is judged by its
    beat. The claim decides who executes; whether the owner still exists decides
    who is still alive.
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
        #: Who this manager claims runs as; the string stored on the run
        #: (``owner``) and compared by reconciliation.
        self._owner = _process_identity()
        self._lock = threading.Lock()
        #: Serializes event appends from a run's worker threads (a chunk pool
        #: reports from several at once), so the persisted stream has one order.
        self._events_lock = threading.Lock()
        #: Runs this manager is executing, so a waiter does not return on a run's
        #: terminal status before its synchronous effects finish, and so
        #: reconciliation never reaps a run of its own as dead.
        self._live: set[int] = set()
        #: Meeting + options for runs enqueued in this process, so a live run
        #: does not pay to re-read what it just wrote.
        self._pending: dict[int, tuple[Meeting, PipelineOptions]] = {}
        #: The thread that keeps :attr:`_live` runs' heartbeats fresh while they
        #: execute, and the runs whose beat failed (reported once each).
        self._heartbeat: threading.Thread | None = None
        self._beat_failed: set[int] = set()
        #: One cancel signal per run this manager is executing (RUN-04). The
        #: pipeline gets it while it runs; a request that arrives before the run
        #: starts is carried by the registry instead (``cancel_requested_at``),
        #: and the beat below picks that up.
        self._cancels: dict[int, threading.Event] = {}
        self._wake = threading.Condition()
        self._scheduler: threading.Thread | None = None
        self._stopping = False
        # A run the process died in the middle of is not running any more. Do
        # this before anything can drain the queue.
        self.reconcile()

    # --- startup reconciliation -------------------------------------------- #
    def reconcile(self) -> list[int]:
        """Move every ``running`` run no live owner holds to ``interrupted``.

        Returns the reconciled run ids. The reason is recorded on the run (its
        ``error``) and logged; a meeting left ``running`` follows its run. Then
        the queue is drained, so **queued** work from a previous process is not
        lost by the restart.

        A run is left alone when a **live owner** holds it (RUN-02): this manager
        is executing it, or its owner is a process on this host that still exists
        (see :meth:`_is_dead`). Everything else is an orphan and becomes
        ``interrupted``: an owner process that is gone, no heartbeat at all (a run
        recorded before the heartbeat existed, or one whose owner died before its
        first beat), an owner this node cannot probe whose heartbeat has gone
        stale, or a heartbeat its owner stopped refreshing when it died. Without
        that rule a second manager's startup would read the first process's
        in-flight run as dead; with it, a dead process's run still becomes honest.

        A process that *exists* is believed over its heartbeat, so a run whose
        owner is stalled holds its meeting and the node rather than being declared
        an orphan: that is deliberate, and it is why the two cases (a killed
        process, a stalled one) do not need to be told apart by a deadline. The
        *write* is a compare-and-swap against the ownership and the heartbeat the
        decision was based on — the store's own
        :meth:`~clear_record.service.store.Registry.interrupt_run` — so an owner
        that beats while the decision is being made keeps its run: a stale
        observation is reported and the row is left alone.
        """
        interrupted = self._reap_dead_runs(self._running_runs())
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

    def _running_runs(self) -> list[PipelineRun]:
        """Every ``running`` run, with a row this build refuses **quarantined**.

        The read walks rows and stops at the one it cannot read
        (:func:`~clear_record.service.store.Registry._run` validates the stored
        options as it maps them), so one unreadable row would otherwise hide every
        run behind it — and, at startup, stop the node: this is the read
        :meth:`reconcile` makes from the constructor, before any caller can catch
        anything. The row that cannot be read is the row that can never execute
        (see :meth:`_refuse_run`), so it is failed and the read is retried;
        :meth:`_drain` reads the same rows every second and needs the same rule,
        which is why it lives here rather than in either caller.
        """
        while True:
            try:
                return self._registry.runs_with_status("running")
            except MalformedRunOptions as exc:
                if not self._refuse_run(exc):
                    # The quarantine itself could not be written (a locked
                    # database, say): the row is still there and retrying would
                    # spin, so this read reports no live runs and the caller
                    # carries on. The next pass tries again.
                    return []

    def _next_queued_run(self) -> PipelineRun | None:
        """The head of the FIFO, with a row this build refuses quarantined.

        The queued half of :meth:`_running_runs`, for the same reason: the drain's
        read must not stop the queue on one row nobody can read.
        """
        while True:
            try:
                return self._registry.oldest_queued_run()
            except MalformedRunOptions as exc:
                if not self._refuse_run(exc):
                    return None

    def _reap_dead_runs(self, running: list[PipelineRun]) -> list[int]:
        """Mark every run in ``running`` with no live owner ``interrupted``.

        The one place that transition happens, shared by startup reconciliation
        and the drain loop — the loop reaps too, because a run left ``running``
        by a peer that died would otherwise block the node behind a run nobody
        is executing.

        A reap that loses its compare-and-swap is not one of those transitions:
        the row is left exactly as it is and the stale observation is reported
        instead (see :meth:`_report_stale_observation`), so a returned id is a run
        this call really did interrupt.
        """
        now = _dt.datetime.now(_dt.UTC)
        interrupted: list[int] = []
        for run in running:
            if not self._is_dead(run, now):
                continue
            ended_at = _now()
            # The run never reached its own terminal transition, so this is
            # where an interrupted run gets its cost record: the stages it did
            # complete are still in its persisted event stream.
            reaped = self._registry.interrupt_run(
                run.id,
                observed=run,
                ended_at=ended_at,
                # The row's ``error`` is console text, so it is looked up now
                # (the progress summary and the log record keep the message ID).
                error=tr(RESTART_REASON),
                progress=self._progress(
                    run.id, "interrupted", RESTART_REASON, ended_at=ended_at
                ),
            )
            if reaped is None:
                # The compare lost: the row is not the one this decision was
                # based on. The owner finished in the meantime — its own terminal
                # record (and the meeting status that followed it) is the truth,
                # not this snapshot's verdict — or it refreshed its heartbeat,
                # which is the owner saying it is alive after all. Either way this
                # is a **stale observation**, so the run is left exactly as it is
                # and that observation, not the reap that did not happen, is what
                # gets reported.
                self._report_stale_observation(run)
                continue
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
        return interrupted

    def _report_stale_observation(self, run: PipelineRun) -> None:
        """Report a reaper whose compare-and-swap lost, and why.

        The decision was made on a snapshot read from the registry
        (:meth:`~clear_record.service.store.Registry.runs_with_status`), and the
        row moved before the write in the store's
        :meth:`~clear_record.service.store.Registry.interrupt_run` landed: the run
        is untouched, so there is no interruption to announce. The row is re-read
        to say *which* observation went stale, because the two are worth telling
        apart on the log. A run still ``running`` had its owner refresh its
        heartbeat during the decision — that owner is alive, and the snapshot's
        stale beat was the only thing suggesting otherwise — while any other
        status means some earlier transition got there first: the owner's own end,
        or a peer's reap. Both are observations of a row as it is, which is
        exactly why neither is reported as a reap.
        """
        current = self._registry.get_run(run.id)
        log_event(
            "info",
            "runs",
            "run.reconcile_stale",
            run_id=run.id,
            meeting_id=run.meeting_id,
            status="gone" if current is None else current.status,
            observed_heartbeat_at=run.heartbeat_at,
            heartbeat_at=None if current is None else current.heartbeat_at,
        )

    def _is_dead(self, run: PipelineRun, now: _dt.datetime) -> bool:
        """Whether no live process holds ``run`` (RUN-02's ownership evidence).

        The evidence is the owner **process** when this node can see it, and the
        heartbeat when it cannot:

        * The owner's **process** is probed first (see :func:`_local_owner_state`,
          which reads the answer asymmetrically): a pid that is alive holds its
          run — fail closed, because a stalled owner must not free the node or
          admit a second pipeline for the same meeting, and the row says
          ``running`` because that is the truth. A pid that is *gone* makes the
          run an orphan **now**, however fresh its last heartbeat is, which is
          what lets a killed console reconcile at once instead of leaving its run
          and its meeting refused for the heartbeat deadline.
        * When this node cannot probe the owner at all — another host, a run
          recorded before the column existed, a platform without a signal probe —
          the **heartbeat** decides, which is evidence *about* a process rather
          than proof of one. A beat is such evidence only while it is near
          ``now`` in either direction: every writer on one node reads one clock,
          so a beat far ahead means the clock stepped backwards after it was
          written, and reading it as life would pin the queue until the clock
          caught up.

        The cost of failing closed is named rather than hidden: a pid that exists
        but is not the process that claimed the run — a recycled pid, or a zombie
        whose parent has not reaped it — holds the queue until that pid goes away
        (the process ending, or its parent waiting on it). That pid is the
        operator's handle: nothing in this queue will reap a row whose owner
        still exists. Reaping such a row instead needs a node-level liveness
        protocol, and guessing from a heartbeat alone is how a second pipeline
        gets admitted beside live work.

        The owner string is deliberately *not* compared for equality: this
        process's own identity on a row it is not executing means either a second
        manager here (alive, holding) or a recycled pid, and the probe above
        already answers that question better than a string comparison.
        """
        with self._lock:
            if run.id in self._live:
                return False
        local = _local_owner_state(run.owner)
        if local is not None:
            return not local
        beat = _parse_time(run.heartbeat_at)
        if beat is None:
            return True
        return abs((now - beat).total_seconds()) > HEARTBEAT_STALE_S

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
        origin: str,
        resumes: int | None = None,
    ) -> PipelineRun:
        """Enqueue a run at the back of the node's FIFO and return its row.

        The run is durable immediately (``queued``): if it cannot execute yet it
        keeps its place, and a restart still has it. The per-meeting dedupe is
        read from the registry — and, for the race two readers cannot see,
        enforced by the database's partial unique index (revision 0009): a
        submission that loses it is refused with the same message this method
        raises for a known active run — but a *different* meeting now waits
        honestly instead of fighting for the one GPU.

        ``origin`` says which surface started it, one of :data:`RUN_ORIGINS`
        (RUN-02). It is required — a start path that does not name itself would
        record a run whose provenance nobody can trust — and recorded with the
        row, so it survives the restart that ends this process.

        ``resumes`` names the run this one continues (RUN-04). The link is
        recorded with the row and checked by the registry (same meeting, run
        exists); what makes a resume *cheaper* is the chunk cache, which is why a
        resume also starts the run with ``resume=True`` (see :meth:`resume`).

        ``auto`` is the meta the opt-in ``--auto`` / ``--backend auto``
        resolvers produced (see :func:`clear_record.service.auto.resolve_run`):
        the explanations and which fields were chosen automatically. It is
        merged into the run meta, which always records the **resolved** profile
        and decoder knobs, so a finished run is explainable after the fact.
        """
        if origin not in RUN_ORIGINS:
            raise ValueError(f"origin must be one of {RUN_ORIGINS}, got {origin!r}")
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
            self._active_run_refusal(meeting)

        options = dataclasses.replace(
            options or PipelineOptions(), audio_files=tuple(tape_set.paths)
        )
        options, run_meta = self._resolve_glossary(meeting, options)
        try:
            run = self._registry.create_run(
                meeting.id,
                backend=options.backend,
                model=options.model,
                language=options.language,
                options=self._run_meta(options, run_meta, auto),
                run_options=dataclasses.asdict(options),
                origin=origin,
                resumes_run_id=resumes,
            )
        except IntegrityError as exc:
            # The guard above reads, and a read cannot stop the other writer: two
            # submissions that both read "no active run" both come here, and the
            # database's own index (revision 0009) refuses the second row. The
            # refusal is the guard's message, raised here rather than left as an
            # integrity error — this is the layer that owns that message — with
            # the index's error as its cause.
            self._active_run_refusal(meeting, cause=exc)
        with self._lock:
            self._pending[run.id] = (meeting, options)
        log_event(
            "info",
            "runs",
            "run.enqueued",
            run_id=run.id,
            meeting_id=meeting.id,
            origin=origin,
            backend=options.backend,
            model=options.model,
            language=options.language,
        )
        self._ensure_scheduler()
        return run

    # --- cancel and resume (RUN-04) ----------------------------------------- #
    def cancel(self, run_id: int) -> PipelineRun:
        """Cancel a queued or running run, and return its row as it now stands.

        Two different acts, chosen by where the run actually is:

        * **Queued** — nothing is executing it, so the cancel *is* the terminal
          transition: the row becomes ``stopped`` here and now (conditional on
          ``queued``, so a claim racing this cannot both win). The drain never
          picks it up and a restart has nothing to resurrect.
        * **Running** — its owner is executing it in a workspace, so this records
          a **request** (``cancel_requested_at``); when this manager is the
          owner, the run's in-process signal is set straight away, and otherwise
          the owner reads the request on its next heartbeat and stops at its next
          safe boundary. The owner writes ``stopped`` — never the requester,
          because a run that keeps executing must not read as stopped. A stalled
          owner therefore keeps its run ``running``: the fail-closed rule holds,
          and the honest answer to "why is it not stopping?" is that the process
          holding it is not answering.

        Cancelling a run that is already terminal is a no-op (a second click, a
        stale page): the row is returned unchanged. ``KeyError`` for an unknown
        run, so each caller owns its 404.
        """
        run = self._registry.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in ACTIVE_RUN_STATUSES:
            return run

        if run.status == "queued":
            ended_at = _now()
            stopped = self._registry.stop_run(
                run_id,
                ended_at=ended_at,
                progress=self._progress(run_id, "stopped", ended_at=ended_at),
            )
            if stopped is not None:
                with self._lock:
                    # The options this manager kept for the run are for running
                    # it; a cancelled run never runs, and nothing else would ever
                    # drop them.
                    self._pending.pop(run_id, None)
                meeting = self._registry.meeting_by_id(run.meeting_id)
                if meeting is not None and meeting.status == "running":
                    # A cancel before the run started leaves the meeting runnable,
                    # not recorded and not failed.
                    self._registry.set_meeting_status(run.meeting_id, "ready")
                log_event(
                    "info",
                    "runs",
                    "run.cancelled",
                    run_id=run_id,
                    meeting_id=run.meeting_id,
                    status="stopped",
                )
                return stopped
            # A claim won the race: the run is starting, so ask it to stop
            # instead of insisting it never did.
            current = self._registry.get_run(run_id)
            if current is None:
                raise KeyError(run_id)
            if current.status != "running":
                return current

        requested = self._registry.request_cancel(run_id)
        if requested is None:
            # Terminal between the read and the write: report what it is now.
            current = self._registry.get_run(run_id)
            return current if current is not None else run
        # This manager may be the one executing it: then the pipeline stops at
        # its next boundary instead of waiting for the heartbeat to notice.
        self._stop_signal(run_id)
        log_event(
            "info",
            "runs",
            "run.cancel_requested",
            run_id=run_id,
            meeting_id=requested.meeting_id,
            owner=requested.owner,
        )
        return requested

    def resume(self, run_id: int, *, origin: str) -> PipelineRun:
        """Start a new run that continues ``run_id``, re-using its chunk cache.

        The previous run's **own resolved options** are what it continues with:
        the chunk cache is keyed on backend, model, language, glossary and chunk
        plan, so resuming with the same ones is exactly what makes the cached
        chunks reusable. ``resume`` is forced on — a resume that re-decoded
        everything would be a plain re-run wearing the name.

        Only a terminal run can be resumed (a live one is cancelled first), and
        its options must be on the row: a run old enough to predate them cannot be
        reconstructed, and guessing would be worse than saying so. ``KeyError``
        for an unknown run.
        """
        previous = self._registry.get_run(run_id)
        if previous is None:
            raise KeyError(run_id)
        if previous.status in ACTIVE_RUN_STATUSES:
            # Placeholder-free, like the service's other refusals: the console
            # renders the message it is given, and the user is looking at the run.
            raise ValueError(deferred("this run is still in flight; cancel it first"))
        options = self._options_from_row(previous)
        if options is None:
            raise ValueError(
                deferred(
                    "this run did not record the options it ran with, "
                    "so it cannot be resumed"
                )
            )
        meeting = self._registry.meeting_by_id(previous.meeting_id)
        if meeting is None:
            raise ValueError(deferred("the run's meeting no longer exists"))
        run = self.start(
            meeting,
            # The scope is *this* run's assertion about which chunks may be
            # re-decoded; continuing the work means reusing everything the cache
            # can prove, so a resume starts unscoped.
            dataclasses.replace(
                options, resume=True, rerun_sources=None, rerun_range=None
            ),
            origin=origin,
            resumes=previous.id,
        )
        log_event(
            "info",
            "runs",
            "run.resumed",
            run_id=run.id,
            meeting_id=meeting.id,
            resumes_run_id=previous.id,
        )
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

    def _active_run_refusal(
        self, meeting: Meeting, *, cause: BaseException | None = None
    ) -> NoReturn:
        """Refuse a submission because the meeting already has an active run.

        Two paths reach it and they must read identically: the registry guard
        (the run the meeting already has is read, and the submission never
        reaches the queue) and the database's partial unique index (two
        submissions read "no active run" at the same moment and one insert
        loses). The sentence is :data:`RUN_IN_FLIGHT`, the one the JSON API's
        own pre-check answers with as well — the console does not print it: its
        start form re-renders the live run's fragment instead (a refusal it can
        show the user by showing them the run that holds the meeting), and only
        an API client or an agent's tool reads the sentence.

        It **raises** rather than returning the error, so no call site can drop
        the refusal, and ``cause`` carries the index's own ``IntegrityError`` when
        that is what refused — chained, so a traceback says which of the two it
        was.
        """
        self._refuse(meeting, "a run is already in flight")
        raise ValueError(RUN_IN_FLIGHT) from cause

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

        What the reads here decide is only **what to try next**: whether the node
        looks free and which run is the head of the FIFO. Who executes is decided
        by the claim in :meth:`_execute_run`, so a stale read costs a lost claim,
        never a second executor. A ``running`` row no live process holds is reaped
        here as well as at startup — otherwise a peer that died mid-run would
        block the node behind a run nobody is executing.
        """
        while True:
            with self._wake:
                while True:
                    if self._stopping:
                        return
                    running = self._running_runs()
                    if running and self._reap_dead_runs(running):
                        running = self._running_runs()
                    if not running:
                        run = self._next_queued_run()
                        if run is not None:
                            break
                        # Nothing is waiting and nothing is executing, so the
                        # enqueue-time options this manager kept cannot be
                        # needed: their runs were claimed elsewhere, cancelled
                        # by another writer, or finished. The registry is the
                        # truth; this is a cache, and an empty queue is when to
                        # drop it.
                        self._prune_pending()
                    self._wake.wait(timeout=1.0)
            self._execute_run(run)

    def _execute_run(self, run: PipelineRun) -> None:
        try:
            claimed = self._registry.claim_run(run.id, owner=self._owner)
        except (SQLAlchemyError, MalformedRunOptions) as exc:
            # The claim is one statement, and either way it failed this pass:
            # the database would not take it (a locked registry, a disk error) or
            # the row stopped being readable between the drain's read and here.
            # The queue must outlive that — an escaping exception ends
            # ``cr-run-queue``, and the queued work then waits for the next
            # submission or restart — so it is reported and the run is tried
            # again on the next pass. Nothing was written.
            log_event(
                "warning",
                "runs",
                "run.claim_failed",
                run_id=run.id,
                meeting_id=run.meeting_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        with self._lock:
            pending = self._pending.pop(run.id, None)
            lost = claimed is None
            if not lost:
                self._live.add(run.id)
        if lost:
            # Another process got the claim in between, or is already running a
            # run on this node: the loser moves on and the winner's row stands.
            # Nothing of ours was written, so nothing needs undoing — only the
            # in-process options are dropped.
            log_event(
                "info",
                "runs",
                "run.claim_lost",
                run_id=run.id,
                meeting_id=run.meeting_id,
            )
            return
        run = claimed
        # The run's own cancel signal (RUN-04), registered *before* the pipeline
        # starts so a cancel arriving now cannot miss the run and wait for the
        # next heartbeat to be noticed.
        self._signal_for(run.id)
        self._beat_while_live()
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
                ended_at = _now()
                self._registry.update_run(
                    run.id,
                    status="failed",
                    ended_at=ended_at,
                    error=error,
                    progress=self._progress(run.id, "failed", error, ended_at=ended_at),
                )
                # The pipeline's own failure path sets this; a failure raised
                # around that path must not leave the meeting "running".
                self._registry.set_meeting_status(run.meeting_id, "failed")
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
                # The pipeline is done with it: a later cancel is answered from
                # the registry (a terminal row needs no signal).
                self._cancels.pop(run.id, None)

    # --- the liveness heartbeat (RUN-02) ------------------------------------ #
    def _beat_while_live(self) -> None:
        """Keep the heartbeat of this manager's live runs fresh (one thread).

        Started at the claim and retired when the last live run leaves
        :attr:`_live`. The claim wrote the first heartbeat itself, so a claimed
        run is never in a state another process could read as dead.
        """
        with self._lock:
            if self._heartbeat is not None and self._heartbeat.is_alive():
                return
            self._heartbeat = threading.Thread(
                target=self._beat, name="cr-run-heartbeat", daemon=True
            )
            self._heartbeat.start()

    def _beat(self) -> None:
        """Refresh every live run's heartbeat until none is left."""
        while True:
            with self._lock:
                live = sorted(self._live)
                if not live:
                    # Retire inside the critical section: a claim that ran
                    # between the check above and this assignment would
                    # otherwise find a thread that is alive and about to exit,
                    # and its run would never be beaten at all.
                    self._heartbeat = None
                    return
            for run_id in live:
                try:
                    # Both registry calls of one beat live in this guard: the
                    # write that refreshes the heartbeat, and the read that decides
                    # whether the row was taken from under us. A read that raises
                    # (a locked database, say) must not end the thread — where the
                    # pid probe cannot decide, this beat *is* the only evidence a
                    # run is alive, so a dead thread means a still-executing run
                    # looks dead 30 s later and its meeting and the node are freed.
                    # The registry raises SQLAlchemy's errors (ADR-0030), which
                    # wrap the driver's own.
                    landed = self._registry.heartbeat_run(run_id)
                    asked_to_stop = landed and self._registry.cancel_requested(run_id)
                    reaped = not landed and self._was_reaped(run_id)
                except (SQLAlchemyError, MalformedRunOptions) as exc:
                    # Report it once per run and keep beating — the error says the
                    # node cannot *prove* the run is alive, not that it is not.
                    # Both families belong here: the registry raises SQLAlchemy's
                    # errors, and a row this build cannot read raises the options
                    # seam's ``MalformedRunOptions`` from the same guarded calls
                    # (``get_run``), which is a ``ValueError`` and not an
                    # ``SQLAlchemyError``.
                    self._report_beat_failure(run_id, f"{type(exc).__name__}: {exc}")
                    continue
                if asked_to_stop:
                    self._stop_signal(run_id)
                if reaped:
                    # A peer moved a row this manager is still executing: the
                    # pipeline keeps working (no cancellation contract) and our
                    # terminal write still decides what the run says, but the
                    # disagreement should be visible.
                    self._report_beat_failure(
                        run_id, "a peer reconciled this run while it executes"
                    )
            time.sleep(HEARTBEAT_INTERVAL_S)

    def _stop_signal(self, run_id: int) -> None:
        """Set a live run's cancel signal for it (RUN-04).

        The signal itself is per manager and per run; this is the one place the
        *owner* acts on a request that came from elsewhere — the console asking a
        run executing in an agent's MCP server to stop, say. Setting an already
        set event is a no-op, so the heartbeat may call this as often as it likes.
        """
        with self._lock:
            signal = self._cancels.get(run_id)
        if signal is not None:
            signal.set()

    def _was_reaped(self, run_id: int) -> bool:
        """Whether a run left ``running`` because another writer reconciled it.

        Not every beat that fails to land is news: this manager's own terminal
        write lands a moment before the run leaves :attr:`_live`, so the beat
        meets the same "no longer running" a peer's reap produces — a live set
        check cannot tell those apart. The row can: this process never reconciles
        a run it is executing, so an ``interrupted`` status under a live run is a
        peer's verdict, and that is the one worth reporting.
        """
        row = self._registry.get_run(run_id)
        return row is not None and row.status == "interrupted"

    def _report_beat_failure(self, run_id: int, reason: str) -> None:
        """Log a beat that did not land, once per run (RUN-02).

        Once, not once per beat: a run can execute for hours, and the same
        condition would otherwise fill the rotating log with the same line.
        """
        if run_id in self._beat_failed:
            return
        self._beat_failed.add(run_id)
        log_event(
            "warning",
            "runs",
            "run.heartbeat_failed",
            run_id=run_id,
            reason=reason,
        )

    def _signal_for(self, run_id: int) -> threading.Event:
        """The run's cancel signal, created if this manager has not opened one yet.

        One signal per run *this* manager is executing, so :meth:`cancel` can set
        it from another thread and the pipeline can read it from where the work
        happens. Kept per run rather than per manager: a manager that is draining
        a queue must not confuse one run's stop with the next run's start.
        """
        with self._lock:
            signal = self._cancels.get(run_id)
            if signal is None:
                signal = threading.Event()
                self._cancels[run_id] = signal
            return signal

    def _prune_pending(self) -> None:
        """Drop enqueue-time options that can no longer be used (see the drain).

        A run enqueued here and cancelled by *another* writer never reaches
        :meth:`_execute_run`, which is the only other place the entry is dropped;
        without this its ``(Meeting, PipelineOptions)`` would be retained for the
        life of the process.
        """
        with self._lock:
            self._pending.clear()

    def _options_from_row(self, run: PipelineRun) -> PipelineOptions | None:
        """Rebuild the queued run's options from the registry (after a restart).

        The row has already been validated where the registry read it
        (:func:`clear_record.service.run_options.read_run_options`), so this is a
        rebuild and not a repair: every field is present, of the declared type,
        and ``audio_files`` is already the tuple the options declare. Nothing is
        filtered and nothing falls back to a default — the keys that used to be
        dropped here and the fields that used to be silently defaulted are what
        the seam above now refuses.
        """
        if not run.run_options:
            return None
        return PipelineOptions(**run.run_options)

    def _run_pipeline(
        self, run: PipelineRun, meeting: Meeting, options: PipelineOptions
    ) -> None:
        assert meeting.workspace_path is not None
        # The row is already ``running``: the claim wrote that status, its
        # ``started_at`` and its first heartbeat in one conditional update. A
        # second write here would be a second, unconditional source of truth.
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

        sink = RunChannel(
            self._registry, run.id, self._events_lock, self._signal_for(run.id)
        )

        try:
            self._pipeline(meeting.workspace_path, options, sink)
        except RunCancelled:
            # A cancel is an outcome, not a failure (RUN-04): the run stopped
            # where its own signal said to, so it is recorded as ``stopped`` with
            # the stages it did complete, and the meeting goes back to runnable
            # (the chunk cache is what a resume continues from).
            self._finish_stopped(run, meeting)
            return
        except Exception as exc:  # noqa: BLE001 - recorded for the console, not hidden
            error = f"{type(exc).__name__}: {exc}"
            if self._signal_for(run.id).is_set():
                # The run was cancelled while this was in flight, and whatever the
                # stage raised is downstream of that (a child killed mid-decode is
                # the ordinary case). The outcome the user asked for is a stop, so
                # it is recorded as one — with the stage's own words kept, because
                # they explain where the run actually ended.
                self._finish_stopped(run, meeting, error=error)
                return
            ended_at = _now()
            self._registry.update_run(
                run.id,
                status="failed",
                ended_at=ended_at,
                error=error,
                progress=self._progress(run.id, "failed", error, ended_at=ended_at),
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
        ended_at = _now()
        self._registry.update_run(
            run.id,
            status="done",
            ended_at=ended_at,
            progress=self._progress(run.id, "done", ended_at=ended_at),
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

    def _finish_stopped(
        self, run: PipelineRun, meeting: Meeting, *, error: str | None = None
    ) -> None:
        """Record a cancelled run's terminal state (RUN-04).

        ``stopped`` is terminal like the others, but it means something else: the
        run did not fail and it did not finish — it was cancelled, and what it
        completed is in its cost record. The meeting goes back to ``ready`` rather
        than ``failed`` so the next start is a resume of this work, not a fresh
        attempt at a broken one. ``error`` is kept when a stage reported something
        on its way out of a cancel, so the reason the run ended where it did is
        not lost.
        """
        ended_at = _now()
        self._registry.update_run(
            run.id,
            status="stopped",
            ended_at=ended_at,
            error=error,
            progress=self._progress(run.id, "stopped", error, ended_at=ended_at),
        )
        self._registry.set_meeting_status(meeting.id, "ready")
        log_event(
            "info",
            "runs",
            "run.stopped",
            run_id=run.id,
            meeting_id=meeting.id,
            error=error,
        )

    def _refuse_run(self, exc: MalformedRunOptions) -> bool:
        """Take the run whose stored options this build refuses out of the queue.

        The row cannot be read through the registry at all — that is what the
        refusal is — so the ordinary fail path (which reads the row back) cannot
        be used: the run is failed by id, and the run says why in its own
        ``error``, which is the reader's message plus **the row's own text**
        (``Registry.fail_unreadable_run``), so clearing the unreadable column
        destroys nothing. Its meeting goes to ``failed`` like any other run that
        cannot execute, and the queue carries on with the next run.

        Returns whether a row moved, which is what the read helpers that call it
        need: a ``False`` means the row is still there and they must not retry,
        or they would spin.
        """
        run_id, meeting_id = exc.run_id, exc.meeting_id
        if run_id is None:  # pragma: no cover - the reader always names its run
            return False
        error = str(exc)
        try:
            moved = self._registry.fail_unreadable_run(run_id, error=error)
            meeting = (
                self._registry.meeting_by_id(meeting_id)
                if meeting_id is not None
                else None
            )
            if meeting is not None:
                self._registry.set_meeting_status(meeting.id, "failed")
                self._webhooks.emit(
                    RUN_FAILED,
                    project_id=meeting.project_id,
                    meeting_id=meeting.id,
                    run_id=run_id,
                )
            log_event(
                "error",
                "runs",
                "run.failed",
                run_id=run_id,
                meeting_id=meeting_id,
                error=error,
            )
            return moved
        except Exception as failure:  # noqa: BLE001 - the queue must survive this
            # The one thing this must not do is raise: it runs *inside* the
            # drain's own handler, where raising is what killed the queue before.
            # A registry that cannot take the write is retried on the next pass,
            # which is what the ``False`` tells the caller.
            log_event(
                "error",
                "runs",
                "run.refuse_failed",
                run_id=run_id,
                error=f"{type(failure).__name__}: {failure}",
            )
            return False

    def _fail_unrunnable(
        self, run: PipelineRun, meeting: Meeting | None, error: str
    ) -> None:
        """Fail a queued run that cannot execute at all (e.g. its tape set is gone)."""
        ended_at = _now()
        self._registry.update_run(
            run.id,
            status="failed",
            ended_at=ended_at,
            error=error,
            progress=self._progress(run.id, "failed", error, ended_at=ended_at),
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

    def _progress(
        self,
        run_id: int,
        status: str,
        error: str | None = None,
        *,
        ended_at: str | None = None,
    ) -> dict:
        """The recorded progress summary for a terminal transition, with its cost.

        The cost sub-record (RUN-01) is written for **every** terminal outcome
        — done, failed, interrupted — so a run that stopped early still says
        which stages it completed. Ratios are never stored here: a display
        derives them (see :func:`estimate_eta_s`).
        """
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
            "cost": self._cost_record(run_id, ended_at or _now(), events),
        }

    def _cost_record(self, run_id: int, ended_at: str, events: list[JobEvent]) -> dict:
        """The raw cost primitives of a run that stopped (RUN-01).

        The per-stage wall-clock is read back from the terminal event each stage
        already emits (``JobEvent.elapsed_s``), so no stage needed new
        instrumentation; the chunk economy and the audio seconds come from the
        transcript meta the transcribe stage persists. A primitive the run never
        produced stays ``None`` — the record is a measurement, not a guess.
        """
        row = self._registry.get_run(run_id)
        stages: dict[str, float | None] = dict.fromkeys(_STAGES)
        finished: set[str] = set()
        for event in events:
            if event.stage not in stages or event.elapsed_s is None:
                continue
            stages[event.stage] = event.elapsed_s
            if event.done:
                finished.add(event.stage)
            else:
                finished.discard(event.stage)
        # ``segments.json`` is written at the **end** of the transcribe stage,
        # and transcribe's own terminal event is emitted by the chunk pool
        # *before* that write (``cli/stages.py``). Only a stage that runs
        # strictly after the write proves the transcript on disk is this run's:
        # reconcile and export both do. A run that finished transcribe and then
        # died before the write would otherwise report the previous run's
        # transcript as its own.
        meta = (
            self._segments_meta(row)
            if "reconcile" in finished or "export" in finished
            else {}
        )
        report = meta.get("chunk_report")
        report = report if isinstance(report, dict) else {}
        reused = int_or_none(report.get("reused"))
        redecoded = int_or_none(report.get("redecoded"))
        chunk_seconds = number_or_none(meta.get("chunk_seconds"))
        if chunk_seconds is None and row is not None:
            chunk_seconds = _options_chunk_seconds(row)
        return {
            "stages": stages,
            "audio_seconds": _audio_seconds(meta),
            "chunks": (
                reused + redecoded
                if reused is not None and redecoded is not None
                else None
            ),
            "chunks_reused": reused,
            "chunks_redecoded": redecoded,
            "backend": meta.get("backend") or (row.backend if row else None),
            "model": meta.get("model") or (row.model if row else None),
            "jobs": int_or_none(meta.get("jobs")),
            "chunk_seconds": chunk_seconds,
            # The transcribe stage's worker-memory measurement, read from
            # the same guarded meta as the transcript: segments.json belongs
            # to whichever run wrote it last, so the axis needs this run's
            # own copy (BENCH-01). A run that never reached the guarded meta
            # carries None/None and reports unknown -- never a previous
            # run's peak.
            "peak_rss_bytes": int_or_none(meta.get("peak_rss_bytes")),
            "peak_rss_reason": (
                meta.get("peak_rss_reason")
                if isinstance(meta.get("peak_rss_reason"), str)
                else None
            ),
            "total_wall_seconds": _wall_seconds(
                row.started_at if row else None, ended_at
            ),
            "machine": machine_description(),
        }

    def _segments_meta(self, row: PipelineRun | None) -> dict:
        """The transcript meta the run's workspace holds (``{}`` when none)."""
        if row is None:
            return {}
        meeting = self._registry.meeting_by_id(row.meeting_id)
        if meeting is None or not meeting.workspace_path:
            return {}
        try:
            _, meta = Workspace.at(meeting.workspace_path).load_segments()
        except (OSError, ValueError, TypeError, AttributeError):
            # No transcript yet (or none this process can read): every
            # transcript-derived primitive is simply unknown.
            return {}
        return meta if isinstance(meta, dict) else {}

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
        """Block until the run is terminal **and** has left the live set.

        A terminal status in the registry is not the end of the run's lifecycle:
        :meth:`_execute_run` keeps the id in :attr:`_live` until
        ``_run_pipeline`` and its synchronous effects (the artifact rows and
        the ``run.finished`` log among them) have all happened. Returning on
        the status alone would let a waiter observe a finished run *before* its
        ``run.finished`` log exists. So wait for both, reading :attr:`_live`
        under the lock. ``_live`` is only populated by the process actually
        executing the run, so a run finished by another process still returns
        on its terminal status.

        Bounded by ``timeout``; on timeout the current state is returned.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = self.require_state(run_id)
            with self._lock:
                live = run_id in self._live
            if state.status in TERMINAL_STATUSES and not live:
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
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_STALE_S",
    "PipelineCallable",
    "PipelineOptions",
    "RESTART_REASON",
    "RUN_IN_FLIGHT",
    "RunManager",
    "RunState",
    "RunSummary",
    "TERMINAL_STATUSES",
    "collect_artifacts",
    "cost_of",
    "estimate_eta_s",
    "int_or_none",
    "number_or_none",
]
