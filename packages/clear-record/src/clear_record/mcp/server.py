"""The stdio MCP server: the service as semantic tools for a user's own agent.

This module is the **adapter** half of ADR-0017: it turns ``clear_record.service``
operations into MCP tools and answers over stdio. Every tool method is a thin
translation of a service call — a lookup, a registry read/write or a run
start — and returns a plain, JSON-serializable ``dict``. All domain behaviour
(validation, status transitions, event recording) stays in the service; none of
it is reimplemented here.

Design notes:

- **Anticipated failures are ``ToolError``.** The SDK returns a ``ToolError`` to
  the model as ``is_error=True`` with its message intact, while an uncaught
  exception is reported only as ``Error executing tool <name>``. So every
  unknown-project / unknown-meeting / missing-tape-set path raises a
  ``ToolError`` naming what was wrong and what is available.
- **Results are structured.** Each tool annotates a ``dict``/``list[dict]``
  return, so the SDK publishes an output schema and an agent gets machine-readable
  values, not prose.
- **BYOK.** No model or provider key is read, required or bundled; the agent
  brings its own (ADR-0017).

The server speaks **stdio** only (the client launches ``clear-record mcp`` as a
subprocess); a network transport is deliberately out of scope for v1.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from clear_record.core import PipelineOptions
from clear_record.service import (
    TASK_KINDS,
    AgentConfig,
    AgentTaskError,
    GlossaryTerm,
    Meeting,
    MeetingAgent,
    ModelNotOnDisk,
    NoBackendAvailable,
    Project,
    Registry,
    Runner,
    RunManager,
    RunState,
    describe_draft,
    read_transcript as _read_transcript,
    resolve_run,
)

SERVER_NAME = "clear-record"

INSTRUCTIONS = (
    "clear-record is a local-first transcription console. Use these tools to read "
    "and edit a project's glossary, manage its meetings and tape sets, keep the "
    "story in project and meeting notes, start and watch pipeline runs (with "
    "explicit profile/backend/model/language, or the opt-in auto / backend auto "
    "resolvers), read the transcript text and list the artifacts a run produced. "
    "Run the three agent tasks (glossary collection, transcript check, minutes) "
    "with run_agent_task, review the drafts with list_agent_drafts / "
    "read_agent_draft, and apply one with accept_agent_draft (or discard it with "
    "reject_agent_draft): an agent task's output is a draft until it is accepted. "
    "Recordings and model weights are local files; "
    "this server is a thin adapter over the same service the web console uses. "
    "For the glossary ↔ transcript tuning loop the agent orchestrates: read the "
    "transcript, refine the terms, then re-run with intent."
)


def _as_dict(obj: Any) -> dict[str, Any]:
    """A dataclass instance as a plain, JSON-serializable dict."""
    return dataclasses.asdict(obj)


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
        *,
        runner: Runner | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.registry = registry
        self.manager = manager if manager is not None else RunManager(registry)
        # The agent-task seam's injection points (tests, embedders); with neither
        # a launch resolves the process config the same way the console does.
        self.runner = runner
        self.config = config

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
    def list_projects(self) -> list[dict[str, Any]]:
        """List every project with its glossary term count."""
        counts = self.registry.term_counts()
        return [
            {**_as_dict(project), "term_count": counts.get(project.slug, 0)}
            for project in self.registry.list_projects()
        ]

    def get_project(self, slug: str) -> dict[str, Any]:
        """Read one project by slug, with its glossary term count."""
        project = self._project(slug)
        return {
            **_as_dict(project),
            "term_count": len(self.registry.list_terms(slug)),
        }

    def update_project(
        self,
        slug: str,
        name: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Update a project's name and/or notes.

        ``notes`` is the agent-writable home for the story the user tells about
        the project; omit a field to leave it unchanged.
        """
        self._project(slug)
        try:
            updated = self.registry.update_project(slug, name=name, notes=notes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _as_dict(updated)

    # --- glossary ---------------------------------------------------------- #
    def list_glossary_terms(
        self, project: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
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
        return [_as_dict(term) for term in terms]

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
    ) -> dict[str, Any]:
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
        return _as_dict(created)

    def update_glossary_term(
        self,
        term_id: int,
        term: str | None = None,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
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
        return _as_dict(updated)

    # --- meetings ---------------------------------------------------------- #
    def list_meetings(self, project: str | None = None) -> list[dict[str, Any]]:
        """List meetings, newest first, optionally scoped to one project."""
        if project is not None:
            self._project(project)
        return [_as_dict(m) for m in self.registry.list_meetings(project)]

    def get_meeting(self, project: str, meeting: str) -> dict[str, Any]:
        """Read one meeting by project and meeting slug, with its latest tape set."""
        found = self._meeting(project, meeting)
        tape_set = self.registry.latest_recording_set(found.id)
        return {**_as_dict(found), "tapes": list(tape_set.paths) if tape_set else []}

    def create_meeting(
        self,
        project: str,
        title: str,
        recorded_at: str | None = None,
        workspace_path: str | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
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
        return _as_dict(created)

    def update_meeting(
        self,
        project: str,
        meeting: str,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Update a meeting's title and/or notes (the meeting-level story).

        ``notes`` is where the user's narrative about this meeting lives; omit a
        field to leave it unchanged, pass an empty string to clear notes.
        """
        found = self._meeting(project, meeting)
        try:
            updated = self.registry.update_meeting(found.id, title=title, notes=notes)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _as_dict(updated)

    def set_meeting_tapes(
        self, project: str, meeting: str, paths: list[str]
    ) -> dict[str, Any]:
        """Set a meeting's tape set to these local audio file paths (latest wins)."""
        found = self._meeting(project, meeting)
        try:
            tape_set = self.registry.set_recording_set(found.id, paths)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "meeting_id": found.id,
            "paths": list(tape_set.paths),
            "created_at": tape_set.created_at,
        }

    # --- pipeline runs ----------------------------------------------------- #
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
    ) -> dict[str, Any]:
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

        The returned dict carries the resolved run plus an ``explanations`` list
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
        result = _as_dict(run)
        result["explanations"] = list(resolved.explanations)
        return result

    def list_runs(self, project: str, meeting: str) -> list[dict[str, Any]]:
        """List a meeting's pipeline runs, newest first."""
        found = self._meeting(project, meeting)
        return [_as_dict(run) for run in self.registry.list_runs(found.id)]

    def run_status(self, run_id: int) -> dict[str, Any]:
        """Read a run's status and progress summary (stage, counts, ETA, error)."""
        run = self.registry.get_run(run_id)
        state = self.manager.state(run_id)
        if run is None and state is None:
            raise ToolError(f"unknown run id {run_id}")
        row = _as_dict(run) if run is not None else {"id": run_id}
        row["progress"] = state.summary() if state is not None else None
        return row

    def run_events(self, run_id: int, after: int = 0) -> dict[str, Any]:
        """Read a run's progress events after a cursor.

        Pass the previous response's ``next`` as ``after`` to page forward without
        re-reading. Events are the run's persisted stream, so a run started by an
        earlier server process replays here too.
        """
        state: RunState | None = self.manager.state(run_id)
        if state is None:
            raise ToolError(f"unknown run id {run_id}")
        events = state.events_since(after)
        return {
            "run_id": run_id,
            "status": state.status,
            "events": [_as_dict(event) for event in events],
            "next": after + len(events),
            "error": state.error,
        }

    # --- artifacts --------------------------------------------------------- #
    def list_artifacts(self, project: str, meeting: str) -> list[dict[str, Any]]:
        """List a meeting's artifacts (transcript, record, exports) with checksums."""
        found = self._meeting(project, meeting)
        return [
            _as_dict(artifact) for artifact in self.registry.list_artifacts(found.id)
        ]

    def read_transcript(
        self,
        project: str,
        meeting: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Read a meeting's transcript as text, optionally sliced for long tapes.

        Returns one ``HH:MM:SS.mmm [speaker] text`` line per segment, the total
        segment count, and a ``next`` cursor (pass it back as ``offset``) — so an
        agent can page a multi-hour tape without reading it whole. Prefers the
        reconciled ``record``; falls back to the raw ``transcript`` segments.
        """
        found = self._meeting(project, meeting)
        try:
            page = _read_transcript(found, offset=offset, limit=limit)
        except (FileNotFoundError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return _as_dict(page)

    # --- agent tasks (ADR-0018) -------------------------------------------- #
    def _agent(self, project: str, meeting: str) -> MeetingAgent:
        found = self._meeting(project, meeting)
        return MeetingAgent(
            self.registry, found, runner=self.runner, config=self.config
        )

    def _draft(self, agent: MeetingAgent, run_id: str):
        draft = agent.draft(run_id)
        if draft is None:
            raise ToolError(
                f"unknown agent draft {run_id!r} for {agent.meeting.slug!r}; "
                "list them with list_agent_drafts"
            )
        return draft

    def list_agent_drafts(self, project: str, meeting: str) -> dict[str, Any]:
        """List a meeting's agent-task drafts with their review state.

        The three task kinds (``glossary_collection``, ``transcript_check``,
        ``minutes``) each produce a **draft**; nothing is applied until an
        explicit ``accept_agent_draft``. ``configured`` says whether a runner is
        available at all (an endpoint, a command template, or one injected by the
        embedder), so a launch that would fail is visible before it is attempted.
        """
        agent = self._agent(project, meeting)
        minutes = agent.minutes_artifact()
        return {
            "tasks": list(TASK_KINDS),
            "configured": agent.configured(),
            "drafts": [describe_draft(draft) for draft in agent.drafts()],
            "minutes": None if minutes is None else _as_dict(minutes),
        }

    def read_agent_draft(
        self, project: str, meeting: str, run_id: str
    ) -> dict[str, Any]:
        """Read one draft: its validated value, its provenance and its review state."""
        agent = self._agent(project, meeting)
        return describe_draft(self._draft(agent, run_id))

    def run_agent_task(self, project: str, meeting: str, kind: str) -> dict[str, Any]:
        """Launch one agent task for a meeting and return its draft.

        ``kind`` is one of ``glossary_collection``, ``transcript_check`` or
        ``minutes``. The task is packaged from the meeting's transcript, the
        project's confirmed-term glossary snapshot and the meeting/project notes,
        then run with the configured runner (or the endpoint/command the config
        names). It needs a transcript in the meeting workspace: run the pipeline
        first. The result is a **draft**; review it with ``read_agent_draft`` and
        apply it with ``accept_agent_draft``.
        """
        agent = self._agent(project, meeting)
        try:
            draft = agent.launch(kind)
        except AgentTaskError as exc:
            raise ToolError(str(exc)) from exc
        return describe_draft(draft)

    def accept_agent_draft(
        self, project: str, meeting: str, run_id: str
    ) -> dict[str, Any]:
        """Accept a draft, promoting it into what its kind produces.

        Promotion is type-specific and idempotent: ``glossary_collection`` adds the
        proposed terms as **candidate** registry terms (confirm them individually
        to bias the decoder), ``transcript_check`` writes the corrected revision
        and its change list as a new artifact (never an in-place overwrite of the
        record), and ``minutes`` writes and registers the meeting's minutes
        document. Re-accepting an already-promoted draft changes nothing.
        """
        agent = self._agent(project, meeting)
        draft = self._draft(agent, run_id)
        try:
            promoted = agent.promote(draft)
        except AgentTaskError as exc:
            raise ToolError(str(exc)) from exc
        return describe_draft(promoted)

    def reject_agent_draft(
        self, project: str, meeting: str, run_id: str
    ) -> dict[str, Any]:
        """Reject a draft, keeping it and its provenance on disk as history."""
        agent = self._agent(project, meeting)
        draft = self._draft(agent, run_id)
        return describe_draft(agent.reject(draft))


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
    "run_agent_task",
    "accept_agent_draft",
    "reject_agent_draft",
)


def build_server(
    registry: Registry,
    manager: RunManager | None = None,
    *,
    name: str = SERVER_NAME,
    runner: Runner | None = None,
    config: AgentConfig | None = None,
) -> MCPServer:
    """Build the MCP server over an opened registry (inject a temp one in tests)."""
    tools = ServiceTools(registry, manager, runner=runner, config=config)
    server: MCPServer = MCPServer(name=name, instructions=INSTRUCTIONS)
    for tool_name in TOOL_NAMES:
        server.add_tool(getattr(tools, tool_name))
    return server


def main(*, data_dir: str | None = None) -> int:
    """Open the registry and serve over stdio until the client disconnects."""
    server = build_server(Registry.open(data_dir=data_dir))
    server.run(transport="stdio")
    return 0


__all__ = [
    "INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_NAMES",
    "ServiceTools",
    "build_server",
    "main",
]
