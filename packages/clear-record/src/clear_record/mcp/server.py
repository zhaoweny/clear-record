"""The stdio MCP server: the service as semantic tools for a user's own agent.

This module is the **adapter** half of ADR-0017: it turns ``clear_record.service``
operations into MCP tools and answers over stdio. Every tool method is a thin
translation of a service call — a lookup, a registry read/write or a run
start — and returns a **declared model** (ADR-0030), so the published output
schema is the shape the tool actually answers with and a result is validated on
the way out. All domain behaviour (validation, status transitions, event
recording) stays in the service; none of it is reimplemented here.

Design notes:

- **Anticipated failures are ``ToolError``.** The SDK returns a ``ToolError`` to
  the model as ``is_error=True`` with its message intact, while an uncaught
  exception is reported only as ``Error executing tool <name>``. So every
  unknown-project / unknown-meeting / missing-tape-set path raises a
  ``ToolError`` naming what was wrong and what is available.
- **Results are structured, and the published schema is the shape the tools
  answer with.** Each tool annotates its return with a declared model from the
  boundary vocabulary — one derived from the domain value it publishes
  (``clear_record.service.schemas``), a shape a service view computes
  (``DraftView``, ``AgentDraftsOut``), or this module's own envelope for one that
  wraps a value (``RunStartedOut``, ``RunStatusOut``, ``RunEventPageOut``) — so
  the SDK publishes a real output schema — the fields, not
  ``additionalProperties`` — and an agent gets machine-readable values, not
  prose.
- **What checks that shape, and where.** The SDK validates the returned value
  against the annotation (``mcp`` 2.2.0, ``convert_result``), but pydantic does
  not revalidate an instance of the model it is handed
  (``revalidate_instances='never'``, its default), so the check is not what holds
  a tool's answer to its declaration: a returned *dict* the declaration does not
  describe is refused, while a constructed **instance** — and every tool here
  returns one — is taken at its word. What holds those is the **construction**:
  :meth:`~clear_record.service.schemas.Shape.of` builds each boundary model with
  ``model_validate``, and that is where a missing or mistyped field fails. The
  edge's check is the net under a payload assembled as a plain dict. So the two
  kinds of disagreement part company at the edge: a **dict** the declaration does
  not describe reaches the model as ``Error executing tool <name>`` — the generic
  form any unexpected exception takes — while an **instance** is not re-checked
  there at all, so a shape that disagrees with its own declaration is caught at
  construction or not at all. Either way it is a bug in this tree, not something
  the agent can act on.
- **No model, no credential.** No model or provider key is read, required or
  bundled: the harness brings its own, and this server never speaks to one
  (ADR-0031).
- **The node is a neighbour, not a dependency.** The adapter is a client of the
  *service*, not of the node's HTTP API: it opens the same registry in this
  process, so its tools answer with or without a node running, and the only thing
  it asks the node is whether it is there (:func:`node_line`). It starts no node —
  neither at startup nor to satisfy a tool call — which is what makes the posture
  it states to an agent (:data:`NODE_POSTURE`) true of the code behind it.
  ADR-0032 keeps this server in process; ADR-0017's transport is unchanged.

The server speaks **stdio** only (the client launches ``clear-record mcp`` as a
subprocess); a network transport is deliberately out of scope for v1.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from clear_record.core import PipelineOptions, node
from clear_record.service import (
    TASK_KINDS,
    AgentDraftsOut,
    ArtifactOut,
    DraftView,
    EventOut,
    GlossaryTerm,
    MalformedRunOptions,
    Meeting,
    MeetingAgent,
    MeetingAgentError,
    MeetingOut,
    MeetingTapesOut,
    ModelNotOnDisk,
    NoBackendAvailable,
    Project,
    PipelineRun,
    ProjectCountOut,
    ProjectOut,
    PromotionError,
    Registry,
    RunManager,
    RunOut,
    RunState,
    RunSummary,
    Shape,
    TapeSetOut,
    TermOut,
    TranscriptOut,
    describe_draft,
    out_model,
    read_transcript as _read_transcript,
    resolve_run,
)

SERVER_NAME = "clear-record"

#: What the adapter is relative to the node, stated for **both** cases at once —
#: a node running, and none — because it is the same answer either way. Which case
#: holds is the other half of the statement, the line :func:`node_line` names
#: beside this one. Here rather than in a docstring because the agent reading
#: ``INSTRUCTIONS`` is the one who needs it: an agent that took "no node is
#: listening" for "these tools cannot work" would stop using a surface that is
#: answering.
NODE_POSTURE = (
    "A clear-record node may or may not be running, and these tools answer the "
    "same either way: they are the same service the node serves, in this process "
    "— one registry, one run queue — rather than a client of the node's HTTP API. "
    "A run started here is enqueued on that one queue, which the node and this "
    "process both drain, so exactly one of them executes it and every surface "
    "reads it back from the same registry. This adapter never starts a node."
)

INSTRUCTIONS = (
    "clear-record is a local-first transcription console. Use these tools to read "
    "and edit a project's glossary, manage its meetings and tape sets, keep the "
    "story in project and meeting notes, start and watch pipeline runs (with "
    "explicit profile/backend/model/language, or the opt-in auto / backend auto "
    "resolvers), read the transcript text and list the artifacts a run produced. "
    "The three LLM-shaped jobs are yours to do: read the transcript with "
    "read_transcript, then write your result for glossary collection, transcript "
    "check or minutes with write_agent_draft — it is stored as a draft with the "
    "author identity you declare. Review drafts with list_agent_drafts / "
    "read_agent_draft; a human applies one with accept_agent_draft (or discards "
    "it with reject_agent_draft), and writing a new version of a draft puts it "
    "back in review. clear-record never calls a model and holds no model "
    "credential. Recordings and model weights are local files; this server is a "
    "thin adapter over the same service the web console uses. For the glossary "
    "↔ transcript tuning loop you orchestrate: read the transcript, write "
    "candidate terms as a draft, then re-run with intent.\n\n" + NODE_POSTURE
)


def node_line() -> str:
    """Which case holds for the node, for the agent — the half that varies.

    The adapter asks through the one node client
    (:func:`clear_record.core.node.ask`): the recorded address, proved by one
    request against it, or the sentence the surfaces state when nothing answers.
    So this line answers *where* the node is; what that means for these
    tools — that they answer either way, and that this adapter starts no node — is
    :data:`NODE_POSTURE`, which the instructions carry whatever this line says.
    English on purpose — the MCP surface is machine-read and is never translated
    (``docs/i18n.md``), so a person's locale cannot leak into an agent's
    instructions.
    """
    try:
        address = node.ask()
    except node.NoNodeError:
        return node.NO_NODE_MESSAGE
    return f"The clear-record node is listening at {address.url}"


def _a_refused_row_is_a_tool_error(method: Callable[..., Any]) -> Callable[..., Any]:
    """Answer a stored row this build refuses with the reader's own message.

    The registry refuses such a row at every read, and an uncaught exception
    reaches an agent only as ``Error executing tool <name>`` — which hides the one
    thing that helps: which run, and which field. The refusal already names both,
    so it becomes the tool error, and the agent can act on it or tell its user.
    """

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except MalformedRunOptions as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


#: The tools' own envelopes: a value the registry produced, plus what the tool
#: learned around it. Derived from the value like the rest of the boundary
#: vocabulary (`clear_record.service.schemas`), so a field the domain type grows
#: is published here without a second declaration.
RunStartedOut = out_model(PipelineRun, name="RunStartedOut", explanations=list[str])
RunStatusOut = out_model(PipelineRun, name="RunStatusOut", progress=RunSummary | None)


class RunEventPageOut(Shape):
    """A page of a run's persisted event stream, with the cursor to continue from.

    Named for *this* edge: the web API publishes a page of the same stream
    (``web.app.RunEventsOut``) carrying the events and the cursor for a run whose
    id and status the caller already has from ``/api/runs/{id}`` — the tool needs
    them because a tool call carries its own arguments and no path, so it reports
    the run's status and error beside the page. Two shapes, two names.
    """

    run_id: int
    status: str
    events: list[EventOut]
    next: int
    error: str | None


class ServiceTools:
    """One method per MCP tool; each is a thin ``clear_record.service`` call.

    Kept as an object (rather than module-level closures) so the tool surface is
    directly callable in tests and the registry/run-manager dependency is
    explicit. :func:`build_server` registers each method as a tool.
    """

    def __init__(
        self,
        registry: Registry,
        manager: RunManager | None = None,
    ) -> None:
        self.registry = registry
        self.manager = manager if manager is not None else RunManager(registry)

    # --- lookup helpers ---------------------------------------------------- #
    def _project(self, slug: str) -> Project:
        project = self.registry.get_project(slug)
        if project is None:
            known = ", ".join(p.slug for p in self.registry.list_projects()) or "(none)"
            raise ToolError(f"unknown project {slug!r}; known projects: {known}")
        return project

    def _meeting(self, project: str, meeting: str) -> Meeting:
        self._project(project)
        found = self.registry.get_meeting(project, meeting)
        if found is None:
            known = (
                ", ".join(m.slug for m in self.registry.list_meetings(project))
                or "(none)"
            )
            raise ToolError(
                f"unknown meeting {meeting!r} in project {project!r}; "
                f"known meetings: {known}"
            )
        return found

    # --- projects ---------------------------------------------------------- #
    def list_projects(self) -> list[ProjectCountOut]:
        """List every project with its glossary term count."""
        counts = self.registry.term_counts()
        return [
            ProjectCountOut.of(project, term_count=counts.get(project.slug, 0))
            for project in self.registry.list_projects()
        ]

    def get_project(self, slug: str) -> ProjectCountOut:
        """Read one project by slug, with its glossary term count."""
        project = self._project(slug)
        return ProjectCountOut.of(
            project, term_count=len(self.registry.list_terms(slug))
        )

    def update_project(
        self,
        slug: str,
        name: str | None = None,
        notes: str | None = None,
    ) -> ProjectOut:
        """Update a project's name and/or notes.

        ``notes`` is the agent-writable home for the story the user tells about
        the project; omit a field to leave it unchanged.
        """
        self._project(slug)
        try:
            updated = self.registry.update_project(slug, name=name, notes=notes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return ProjectOut.model_validate(updated)

    # --- glossary ---------------------------------------------------------- #
    def list_glossary_terms(
        self, project: str | None = None, status: str | None = None
    ) -> list[TermOut]:
        """List glossary terms, optionally filtered by project slug and status.

        ``status`` is one of ``candidate``, ``confirmed`` or ``retired``; omit it
        for every term in every project.
        """
        if project is not None:
            self._project(project)
        try:
            terms = self.registry.list_terms(project, status=status)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return [TermOut.model_validate(term) for term in terms]

    def add_glossary_term(
        self,
        project: str,
        term: str,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str = "candidate",
        added_by: str = "human",
        notes: str | None = None,
    ) -> TermOut:
        """Add a term to a project's glossary.

        An agent's suggestions should use ``added_by='agent'`` and the default
        ``status='candidate'`` so they are not treated as truth until reviewed.
        """
        self._project(project)
        try:
            created = self.registry.add_term(
                project,
                term,
                reading=reading,
                aliases=aliases,
                definition=definition,
                status=status,
                added_by=added_by,
                notes=notes,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return TermOut.model_validate(created)

    def update_glossary_term(
        self,
        term_id: int,
        term: str | None = None,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> TermOut:
        """Update one glossary term by its numeric id (from ``list_glossary_terms``)."""
        try:
            updated: GlossaryTerm = self.registry.update_term(
                term_id,
                term=term,
                reading=reading,
                aliases=aliases,
                definition=definition,
                status=status,
                notes=notes,
            )
        except KeyError as exc:
            raise ToolError(f"unknown glossary term id {term_id}") from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return TermOut.model_validate(updated)

    # --- meetings ---------------------------------------------------------- #
    def list_meetings(self, project: str | None = None) -> list[MeetingOut]:
        """List meetings, newest first, optionally scoped to one project."""
        if project is not None:
            self._project(project)
        return [
            MeetingOut.model_validate(meeting)
            for meeting in self.registry.list_meetings(project)
        ]

    def get_meeting(self, project: str, meeting: str) -> MeetingTapesOut:
        """Read one meeting by project and meeting slug, with its latest tape set."""
        found = self._meeting(project, meeting)
        tape_set = self.registry.latest_recording_set(found.id)
        return MeetingTapesOut.of(found, tapes=list(tape_set.paths) if tape_set else [])

    def create_meeting(
        self,
        project: str,
        title: str,
        recorded_at: str | None = None,
        workspace_path: str | None = None,
        slug: str | None = None,
    ) -> MeetingOut:
        """Create a meeting inside a project.

        ``workspace_path`` is the directory the pipeline runs in; set it here (or
        before a run) so the meeting is runnable.
        """
        self._project(project)
        try:
            created = self.registry.create_meeting(
                project,
                title,
                recorded_at=recorded_at,
                workspace_path=workspace_path,
                slug=slug,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return MeetingOut.model_validate(created)

    def update_meeting(
        self,
        project: str,
        meeting: str,
        title: str | None = None,
        notes: str | None = None,
    ) -> MeetingOut:
        """Update a meeting's title and/or notes (the meeting-level story).

        ``notes`` is where the user's narrative about this meeting lives; omit a
        field to leave it unchanged, pass an empty string to clear notes.
        """
        found = self._meeting(project, meeting)
        try:
            updated = self.registry.update_meeting(found.id, title=title, notes=notes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return MeetingOut.model_validate(updated)

    def set_meeting_tapes(
        self, project: str, meeting: str, paths: list[str]
    ) -> TapeSetOut:
        """Set a meeting's tape set to these local audio file paths (latest wins)."""
        found = self._meeting(project, meeting)
        try:
            tape_set = self.registry.set_recording_set(found.id, paths)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return TapeSetOut.model_validate(tape_set)

    # --- pipeline runs ----------------------------------------------------- #
    @_a_refused_row_is_a_tool_error
    def start_run(
        self,
        project: str,
        meeting: str,
        profile: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        language: str | None = None,
        glossary: str | None = None,
        auto: bool = False,
    ) -> RunStartedOut:
        """Start the pipeline for a meeting's latest tape set, with intent.

        The run executes in the background; read it with ``run_status`` and
        ``run_events``. Any of ``profile``, ``backend``, ``model``, ``language``
        and ``glossary`` (a glossary file path) may be set to re-run
        deliberately. They are resolved by the service's explainable resolvers
        (``clear_record.service.resolve_run``, the same code the console uses),
        so the reported result is what actually runs:

        - an explicit value, then the ``CR_*`` environment, then the profile,
          then the built-in default (``clear_record.core.resolve_options``);
        - ``backend='auto'`` replaces the sentinel with the first available ASR
          backend;
        - ``auto=True`` (the opt-in ``--auto``) fills the profile, model and
          per-speaker attribution **only where left unset**, never downloading a
          model.

        The returned run carries the resolved fields plus an ``explanations`` list
        — the resolvers' own words (also recorded in the run meta under
        ``options.auto`` / ``options.backend_auto``) — so the caller sees what
        ``auto`` decided instead of guessing.

        Refused (with a reason) when the meeting has no workspace, no tape set,
        a run is already in flight, no ASR backend is available for
        ``backend='auto'``, or ``auto`` recommends a model that is not on disk.

        **Glossary default.** Omit ``glossary`` to apply the **project's
        confirmed-term snapshot**: it is written to the meeting workspace's
        ``glossary.txt`` and used by this run, so a glossary edit reaches the
        next run with no extra wiring (the tuning loop). Pass ``glossary`` to
        override it explicitly. The returned run meta records
        ``options.glossary`` and ``options.glossary_sha256`` so a re-run is
        explainable.
        """
        found = self._meeting(project, meeting)
        explicit: dict[str, Any] = {}
        if backend is not None:
            explicit["backend"] = backend
        if model is not None:
            explicit["model"] = model
        if language is not None:
            explicit["language"] = language
        if glossary is not None:
            explicit["glossary"] = glossary
        try:
            resolved = resolve_run(
                PipelineOptions(**explicit),
                profile=profile,
                auto=auto,
                directory=found.workspace_path,
            )
        except ModelNotOnDisk as exc:
            raise ToolError(str(exc)) from exc
        except NoBackendAvailable as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        try:
            run = self.manager.start(
                found, resolved.options, auto=resolved.meta, origin="mcp"
            )
        except ValueError as exc:
            raise ToolError(
                f"cannot start a run for {project}/{meeting}: {exc}"
            ) from exc
        return RunStartedOut.of(run, explanations=list(resolved.explanations))

    @_a_refused_row_is_a_tool_error
    def list_runs(self, project: str, meeting: str) -> list[RunOut]:
        """List a meeting's pipeline runs, newest first."""
        found = self._meeting(project, meeting)
        return [RunOut.model_validate(run) for run in self.registry.list_runs(found.id)]

    @_a_refused_row_is_a_tool_error
    def run_status(self, run_id: int) -> RunStatusOut:
        """Read a run's status and progress summary (stage, counts, ETA, error).

        ``progress`` is the run's summary read from the registry, so a run another
        node started, or one already finished, reports the same way as one this
        process is executing.
        """
        run = self.registry.get_run(run_id)
        if run is None:
            raise ToolError(f"unknown run id {run_id}")
        state = self.manager.state(run_id)
        return RunStatusOut.of(
            run, progress=state.summary() if state is not None else None
        )

    @_a_refused_row_is_a_tool_error
    def run_events(self, run_id: int, after: int = 0) -> RunEventPageOut:
        """Read a run's stream after a cursor: its progress, and the words it reported.

        Pass the previous response's ``next`` as ``after`` to page forward without
        re-reading. Events are the run's *persisted* stream and carry both: a
        stage's mid-stage lines, each pass's summary and the data items it
        produced arrive as events whose ``message`` is the text, while a pure
        progress report carries no message at all — so a run an earlier server
        process started replays here too (the live run is a queue claim, not a
        copy of the stream: ``RunManager.state`` reads the registry's rows and
        events on every call).
        """
        state: RunState | None = self.manager.state(run_id)
        if state is None:
            raise ToolError(f"unknown run id {run_id}")
        events = state.events_since(after)
        return RunEventPageOut(
            run_id=run_id,
            status=state.status,
            events=[EventOut.model_validate(event) for event in events],
            next=after + len(events),
            error=state.error,
        )

    # --- artifacts --------------------------------------------------------- #
    def list_artifacts(self, project: str, meeting: str) -> list[ArtifactOut]:
        """List a meeting's artifacts (transcript, record, exports) with checksums."""
        found = self._meeting(project, meeting)
        return [
            ArtifactOut.model_validate(artifact)
            for artifact in self.registry.list_artifacts(found.id)
        ]

    def read_transcript(
        self,
        project: str,
        meeting: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> TranscriptOut:
        """Read a meeting's transcript as text, optionally sliced for long tapes.

        Returns one ``HH:MM:SS.mmm [speaker] text`` line per segment, the total
        segment count, and a ``next`` cursor (pass it back as ``offset``) — so an
        agent can page a multi-hour tape without reading it whole. A time is
        signed when it is negative: the reconciled ``record`` is on the reference
        clock, so a source that started before the reference reads
        ``-00:01:54.365``. Prefers the ``record``; falls back to the raw
        ``transcript`` segments, whose times are each source's own (``reconcile``
        is what shifts them onto the reference).
        """
        found = self._meeting(project, meeting)
        try:
            page = _read_transcript(found, offset=offset, limit=limit)
        except (FileNotFoundError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return TranscriptOut.model_validate(page)

    # --- drafts (ADR-0031: the harness writes, a human decides) ------------ #
    def _agent(self, project: str, meeting: str) -> MeetingAgent:
        found = self._meeting(project, meeting)
        return MeetingAgent(self.registry, found)

    def _draft(self, agent: MeetingAgent, draft_id: str):
        draft = agent.draft(draft_id)
        if draft is None:
            raise ToolError(
                f"unknown agent draft {draft_id!r} for {agent.meeting.slug!r}; "
                "list them with list_agent_drafts"
            )
        return draft

    def list_agent_drafts(self, project: str, meeting: str) -> AgentDraftsOut:
        """List a meeting's draft chains, each with its versions and review state.

        A draft is a **version chain with author provenance**: every version
        records the ``author`` its writer declared and when it was written, and
        the human's accept/reject is recorded on the version it decided. The
        three kinds (``glossary_collection``, ``transcript_check``, ``minutes``)
        are what a harness writes with ``write_agent_draft``; nothing is applied
        until a human accepts it with ``accept_agent_draft``. ``minutes`` is the
        meeting's accepted minutes artifact, or ``null`` until one exists.
        """
        agent = self._agent(project, meeting)
        minutes = agent.minutes_artifact()
        return AgentDraftsOut(
            kinds=list(TASK_KINDS),
            drafts=[describe_draft(draft) for draft in agent.drafts()],
            minutes=None if minutes is None else ArtifactOut.model_validate(minutes),
        )

    def read_agent_draft(self, project: str, meeting: str, draft_id: str) -> DraftView:
        """Read one draft chain: its newest value, its versions and its review state."""
        agent = self._agent(project, meeting)
        return describe_draft(self._draft(agent, draft_id))

    def write_agent_draft(
        self,
        project: str,
        meeting: str,
        kind: str,
        value: dict[str, Any],
        author: str = "agent",
        draft_id: str | None = None,
    ) -> DraftView:
        """Write what you produced for a meeting as a draft version, and record who wrote it.

        ``kind`` is one of ``glossary_collection``, ``transcript_check`` or
        ``minutes``, and ``value`` is the JSON object the harness produced:

        - ``glossary_collection`` — ``{"terms": [{"term", "reading", "aliases",
          "definition", "evidence"}]}``: candidate terms read from the meeting's
          transcript (``read_transcript``). Accepting adds them as **candidate**
          glossary terms — confirm them individually to bias the decoder.
        - ``transcript_check`` — ``{"revision": "<the corrected transcript>",
          "changes": [{"before", "after", "reason"}]}``. Accepting writes the
          revision as a new ``transcript_revision`` artifact and never overwrites
          the reconciled record.
        - ``minutes`` — ``{"body": "<markdown>", "attendees", "decisions",
          "actions", "meeting", "project"}``. Accepting writes and registers the
          meeting's ``minutes`` artifact.

        ``author`` is the identity you declare — it is the draft's only
        provenance, because clear-record did not produce the value and never saw
        your model. Pass ``draft_id`` to append a version to an existing chain
        (a re-run, a refinement) instead of opening a new one; a new version puts
        the draft back in review, so it is never accepted on the strength of an
        earlier version. The result is a **draft** until a human accepts it.
        """
        agent = self._agent(project, meeting)
        try:
            draft = agent.write(kind, value, author=author, draft_id=draft_id)
        except MeetingAgentError as exc:
            raise ToolError(str(exc)) from exc
        return describe_draft(draft)

    def accept_agent_draft(
        self,
        project: str,
        meeting: str,
        draft_id: str,
        version: int,
        author: str = "human",
    ) -> DraftView:
        """Accept one version of a draft, promoting it into what its kind produces.

        ``version`` is **required**: it is the version number you read
        (``read_agent_draft`` reports it, and the chain's versions are numbered
        from 1). A decision is never applied to a version you did not name: one
        that is no longer the newest when the decision arrives is refused, because
        a harness may append a version between your read and your decision, and a
        decision must apply to text that was actually reviewed.

        Promotion is type-specific and idempotent: ``glossary_collection`` adds the
        proposed terms as **candidate** registry terms, ``transcript_check`` writes
        the corrected revision and its change list as a new artifact (never an
        in-place overwrite of the record), and ``minutes`` writes and registers the
        meeting's minutes document. The acceptance records ``author`` as the human
        who decided it, and a version that already carries a decision is returned
        unchanged — deciding twice runs no second side effect.
        """
        agent = self._agent(project, meeting)
        draft = self._draft(agent, draft_id)
        try:
            promoted = agent.promote(draft, author=author, version=version)
        except (MeetingAgentError, PromotionError) as exc:
            raise ToolError(str(exc)) from exc
        return describe_draft(promoted)

    def reject_agent_draft(
        self,
        project: str,
        meeting: str,
        draft_id: str,
        version: int,
        author: str = "human",
    ) -> DraftView:
        """Reject one version of a draft, keeping the whole chain on disk as history.

        ``version`` is **required**, as in ``accept_agent_draft``: it is the
        version you read, and one that is no longer the newest is refused rather
        than recording a decision on a version you did not see.
        """
        agent = self._agent(project, meeting)
        draft = self._draft(agent, draft_id)
        try:
            rejected = agent.reject(draft, author=author, version=version)
        except MeetingAgentError as exc:
            raise ToolError(str(exc)) from exc
        return describe_draft(rejected)


#: The tool methods, in the order they are registered. Kept explicit so the
#: surface is reviewable in one place and a rename cannot silently change a name.
TOOL_NAMES: tuple[str, ...] = (
    "list_projects",
    "get_project",
    "update_project",
    "list_glossary_terms",
    "add_glossary_term",
    "update_glossary_term",
    "list_meetings",
    "get_meeting",
    "create_meeting",
    "update_meeting",
    "set_meeting_tapes",
    "start_run",
    "list_runs",
    "run_status",
    "run_events",
    "list_artifacts",
    "read_transcript",
    "list_agent_drafts",
    "read_agent_draft",
    "write_agent_draft",
    "accept_agent_draft",
    "reject_agent_draft",
)


def build_server(
    registry: Registry,
    manager: RunManager | None = None,
    *,
    name: str = SERVER_NAME,
    node_note: str | None = None,
) -> MCPServer:
    """Build the MCP server over an opened registry (inject a temp one in tests).

    ``node_note`` is the line that states **which case holds** for the node — where
    it answers, or that none does (:func:`node_line`; ``main`` passes it). The
    adapter's own posture toward the node needs no note, because it is the same in
    both cases and :data:`INSTRUCTIONS` already carries it. Omitted, the
    instructions are the adapter's own text, which is what a test that constructs
    the server directly wants: this adapter works over the service in process, with
    a node or without one, and starts none either way.
    """
    tools = ServiceTools(registry, manager)
    instructions = (
        INSTRUCTIONS if node_note is None else f"{INSTRUCTIONS}\n\n{node_note}"
    )
    server: MCPServer = MCPServer(name=name, instructions=instructions)
    for tool_name in TOOL_NAMES:
        server.add_tool(getattr(tools, tool_name))
    return server


def main(*, data_dir: str | None = None) -> int:
    """Open the registry and serve over stdio until the client disconnects."""
    server = build_server(Registry.open(data_dir=data_dir), node_note=node_line())
    server.run(transport="stdio")
    return 0


__all__ = [
    "INSTRUCTIONS",
    "NODE_POSTURE",
    "SERVER_NAME",
    "TOOL_NAMES",
    "RunEventPageOut",
    "RunStartedOut",
    "RunStatusOut",
    "ServiceTools",
    "build_server",
    "main",
    "node_line",
]
