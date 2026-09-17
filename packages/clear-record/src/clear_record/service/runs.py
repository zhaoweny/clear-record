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
  replays and the "one run per meeting" guard still holds. A run whose owner
  died is reconciled to ``interrupted`` (distinct from ``failed`` — the node
  died, the work did not necessarily fail).
* **One run per node, one claim.** :meth:`RunManager.start` **enqueues** a run
  (``queued``); a scheduler drains the FIFO, and the move to ``running`` is a
  single conditional update in the registry, so the console, an agent's MCP
  server and a CLI all share one queue and exactly one of them executes a run.
  The queue is the registry's, so a restart does not lose queued work.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import platform as _platform
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from clear_record.cli import stages
from clear_record.cli.workspace import Workspace
from clear_record.core import EventSink, JobEvent, PipelineOptions
from clear_record.core.diagnostics import log_event
from clear_record.core.i18n import deferred
from clear_record.core.pipeline import pipeline_spec
from clear_record.service.diagnostics import machine_description
from clear_record.service.glossary import (
    project_snapshot,
    snapshot_from_text,
    write_snapshot,
)
from clear_record.service.models import RUN_ORIGINS, Meeting, PipelineRun
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
    host, and to grow a node column from later (Q15).
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
    if run.status not in ("queued", "running"):
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

    The queue is **cross-process** (RUN-02): a queued run is claimed by one
    conditional update (:meth:`~clear_record.service.store.Registry.claim_run`),
    so the console, an agent's MCP server and a CLI can all write to the same
    registry and exactly one of them executes a run. The in-process pieces are a
    fast path, not the guarantee: :attr:`_pending` saves re-reading what this
    process just wrote, and :attr:`_live` says what this process is executing.

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
        process, a stalled one) do not need to be told apart by a deadline.
        """
        interrupted = self._reap_dead_runs(self._registry.runs_with_status("running"))
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

    def _reap_dead_runs(self, running: list[PipelineRun]) -> list[int]:
        """Mark every run in ``running`` with no live owner ``interrupted``.

        The one place that transition happens, shared by startup reconciliation
        and the drain loop — the loop reaps too, because a run left ``running``
        by a peer that died would otherwise block the node behind a run nobody
        is executing.
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
                ended_at=ended_at,
                error=RESTART_REASON,
                progress=self._progress(
                    run.id, "interrupted", RESTART_REASON, ended_at=ended_at
                ),
            )
            if reaped is None:
                # The owner finished in the meantime: the run is not an orphan,
                # and its own terminal record (and the meeting status that
                # followed it) is the truth, not this snapshot's verdict.
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
    ) -> PipelineRun:
        """Enqueue a run at the back of the node's FIFO and return its row.

        The run is durable immediately (``queued``): if it cannot execute yet it
        keeps its place, and a restart still has it. The per-meeting dedupe is
        read from the registry, so the same tape set is never enqueued twice
        concurrently — but a *different* meeting now waits honestly instead of
        fighting for the one GPU.

        ``origin`` says which surface started it, one of :data:`RUN_ORIGINS`
        (RUN-02). It is required — a start path that does not name itself would
        record a run whose provenance nobody can trust — and recorded with the
        row, so it survives the restart that ends this process.

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
            origin=origin,
        )
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
                    running = self._registry.runs_with_status("running")
                    if running and self._reap_dead_runs(running):
                        running = self._registry.runs_with_status("running")
                    if not running:
                        run = self._registry.oldest_queued_run()
                        if run is not None:
                            break
                    self._wake.wait(timeout=1.0)
            self._execute_run(run)

    def _execute_run(self, run: PipelineRun) -> None:
        claimed = self._registry.claim_run(run.id, owner=self._owner)
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
                    landed = self._registry.heartbeat_run(run_id)
                except sqlite3.Error as exc:
                    # A liveness thread must not die on a transient registry
                    # error: another process would then read this run as dead and
                    # reap it while it is still executing. Report it once per run
                    # and keep beating — the error says the node cannot *prove*
                    # the run is alive, not that it is not.
                    self._report_beat_failure(run_id, f"{type(exc).__name__}: {exc}")
                    continue
                if not landed and self._was_reaped(run_id):
                    # A peer moved a row this manager is still executing: the
                    # pipeline keeps working (no cancellation contract) and our
                    # terminal write still decides what the run says, but the
                    # disagreement should be visible.
                    self._report_beat_failure(
                        run_id, "a peer reconciled this run while it executes"
                    )
            time.sleep(HEARTBEAT_INTERVAL_S)

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

        def sink(event: JobEvent) -> None:
            with self._events_lock:
                self._registry.add_run_event(run.id, event)

        try:
            self._pipeline(meeting.workspace_path, options, sink)
        except Exception as exc:  # noqa: BLE001 - recorded for the console, not hidden
            error = f"{type(exc).__name__}: {exc}"
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
    "RunManager",
    "RunState",
    "TERMINAL_STATUSES",
    "collect_artifacts",
    "cost_of",
    "estimate_eta_s",
    "int_or_none",
    "number_or_none",
]
