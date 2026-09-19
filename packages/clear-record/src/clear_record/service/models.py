"""Domain values of the clear-record registry.

Plain frozen dataclasses (no third-party imports) so the service layer stays as
light as the core. ``clear_record.core`` owns the *audio* domain (sources,
segments, records); this module owns the *project* domain (projects and glossary
terms), which only exists for the web/service surface.
"""

from __future__ import annotations

import dataclasses

#: A glossary term's lifecycle. ``candidate`` is what an agent's draft produces;
#: ``confirmed`` is owner-accepted truth; ``retired`` is kept for history.
TERM_STATUSES = ("candidate", "confirmed", "retired")

#: Who a term came from. A human edit and an agent draft are never conflated.
TERM_AUTHORS = ("human", "agent")


@dataclasses.dataclass(frozen=True)
class Project:
    """A durable container for meetings, a glossary and archives."""

    id: int
    slug: str
    name: str
    notes: str
    default_archive_root: str | None
    created_at: str


@dataclasses.dataclass(frozen=True)
class GlossaryTerm:
    """One term in a project's glossary.

    ``project_slug`` is joined in on reads so a cross-project table can show
    where a term belongs without a second query.
    """

    id: int
    project_id: int
    project_slug: str
    term: str
    reading: str | None
    aliases: str | None
    definition: str | None
    status: str
    added_by: str
    notes: str | None
    created_at: str


#: A meeting's lifecycle. ``new`` has no tapes yet; ``ready`` has a tape set;
#: ``running`` has a live pipeline run; ``recorded`` has a reconciled record.
#: ``interrupted`` is a run the node died in the middle of (startup
#: reconciliation), which is honest about the node, not the work: see
#: :data:`RUN_STATUSES`.
MEETING_STATUSES = ("new", "ready", "running", "recorded", "failed", "interrupted")

#: A pipeline run's lifecycle. ``interrupted`` is distinct from ``failed``: the
#: node (the console process) died while the run was live, so the work did not
#: necessarily fail. A run left ``running`` by a dead process is moved here at
#: startup, and its event stream stays readable.
RUN_STATUSES = ("queued", "running", "done", "failed", "stopped", "interrupted")

#: Which surface **started** a run (RUN-02). ``console`` is the local console UI,
#: ``api`` the HTTP JSON API (a script or an integration), ``mcp`` the stdio MCP
#: server an agent harness drives, ``cli`` the command line. It is recorded when
#: the run is enqueued, so a run's provenance survives the restart that ends its
#: process.
RUN_ORIGINS = ("console", "api", "mcp", "cli")


@dataclasses.dataclass(frozen=True)
class Meeting:
    """One recording session inside a project: its tapes, its record and notes."""

    id: int
    project_id: int
    project_slug: str
    slug: str
    title: str
    recorded_at: str | None
    workspace_path: str | None
    notes: str
    status: str
    created_at: str


@dataclasses.dataclass(frozen=True)
class RecordingSet:
    """The tapes chosen for a meeting (the latest selection wins)."""

    id: int
    meeting_id: int
    paths: tuple[str, ...]
    created_at: str


@dataclasses.dataclass(frozen=True)
class Tape:
    """An **uploaded** tape with the copy's integrity facts (ADR-0024).

    A tape reaches a meeting two ways: a path the user typed (recorded only in
    the meeting's :class:`RecordingSet`) or a file uploaded to the managed
    workspace. The uploaded kind is also recorded here, with the ``sha256`` and
    ``bytes`` measured while streaming it to disk, so storage visibility can
    show what the node actually holds and detect a later change.
    """

    id: int
    meeting_id: int
    path: str
    sha256: str
    bytes: int
    created_at: str


@dataclasses.dataclass(frozen=True)
class PipelineRun:
    """One execution of the pipeline against a meeting's tape set.

    ``status`` moves ``queued → running → done|failed|interrupted``: a run is
    **enqueued** first (the FIFO the node drains one at a time), and only one run
    per node is ``running`` at once. The move to ``running`` is a **conditional
    claim** (:meth:`~clear_record.service.store.Registry.claim_run`), so several
    processes sharing the registry cannot execute the same run. ``interrupted``
    means the process that owned the run died while it was live, which is not the
    same as the pipeline failing.
    """

    id: int
    meeting_id: int
    status: str
    backend: str | None
    model: str | None
    language: str | None
    #: Run meta recorded at start: the glossary path actually used and the
    #: sha256 of that glossary snapshot (plus anything future runs add). It is
    #: what makes a re-run explainable — see ``service.glossary``.
    options: dict | None
    started_at: str | None
    ended_at: str | None
    error: str | None
    created_at: str
    #: The resolved :class:`~clear_record.core.PipelineOptions` a **queued** run
    #: will execute with, serialized at enqueue time. It is what lets a restart
    #: pick a queued run back up with the knobs the user chose (audio files are
    #: re-read from the meeting's latest tape set at execution time).
    run_options: dict | None = None
    #: The terminal summary written when the run ends. Its ``cost`` holds the
    #: run's **raw** cost primitives (RUN-01: per-stage wall-clock, audio
    #: seconds, the chunk economy, backend/model/jobs/chunk seconds, the machine
    #: description) — never a derived ratio. ``None`` means no record: a live
    #: run, or one recorded before the record existed, both read as unknown.
    progress: dict | None = None
    #: The surface that started the run (RUN-02): one of :data:`RUN_ORIGINS`.
    #: ``None`` is a run enqueued before the column existed — read as unknown,
    #: never guessed.
    origin: str | None = None
    #: The process identity that **claimed** the run, written by the claim so a
    #: later reader can say who runs it. The claim is what makes it meaningful:
    #: it is set once, by the winner, and never changes.
    owner: str | None = None
    #: When the owner last proved it was alive. The owner refreshes it while the
    #: run executes; another process's reconciliation treats a missing or stale
    #: heartbeat as a dead owner — that is how a run left by a killed process is
    #: told from one a live peer is executing (RUN-02).
    heartbeat_at: str | None = None
    #: The run this one continues (RUN-04), set when a cancelled or interrupted
    #: run is resumed. ``resumes`` is the link; the *saving* is the chunk cache,
    #: which a resumed run re-uses because it runs the same tape with the same
    #: backend, model, glossary and chunk plan.
    resumes_run_id: int | None = None
    #: When someone asked this run to stop (RUN-04). A *request*, not a state:
    #: only the process executing the run may end it, so this column is what the
    #: owner reads on its next heartbeat before stopping at a safe boundary.
    cancel_requested_at: str | None = None


@dataclasses.dataclass(frozen=True)
class Artifact:
    """A file a run or an agent produced, with its checksum."""

    id: int
    meeting_id: int
    run_id: int | None
    kind: str
    path: str
    sha256: str | None
    bytes: int | None
    produced_by: str
    review_state: str
    created_at: str


@dataclasses.dataclass(frozen=True)
class Archive:
    """An immutable, checksummed copy of a meeting's tapes and record.

    ``root_path`` names the timestamped archive **directory** (the copy), and
    ``manifest_path`` the ``archive.json`` inside it; ``manifest_sha256`` seals
    the manifest so the registry can detect a later edit. The original files
    stay in the user's workspace — an archive is a copy, never a move.
    """

    id: int
    meeting_id: int
    project_id: int
    root_path: str
    manifest_path: str
    manifest_sha256: str
    created_at: str


__all__ = [
    "Archive",
    "Artifact",
    "GlossaryTerm",
    "Meeting",
    "MEETING_STATUSES",
    "PipelineRun",
    "Project",
    "RecordingSet",
    "RUN_ORIGINS",
    "RUN_STATUSES",
    "TERM_AUTHORS",
    "TERM_STATUSES",
    "Tape",
]
