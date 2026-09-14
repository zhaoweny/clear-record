"""The stdio MCP server: the service as semantic tools for a user's own agent.

This module is the **adapter** half of ADR-0017: it turns ``clear_record.service``
operations into MCP tools and answers over stdio. Every tool method is a thin
translation of a service call — a lookup, a registry read/write or a run
start — and returns a plain, JSON-serialisable ``dict``. All domain behaviour
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

from clear_record.core import PipelineOptions, resolve_options
from clear_record.service import (
    TERM_STATUSES,
    GlossaryTerm,
    Meeting,
    Project,
    Registry,
    RunManager,
    RunState,
    read_transcript as _read_transcript,
)

SERVER_NAME = "clear-record"

INSTRUCTIONS = (
    "clear-record is a local-first transcription console. Use these tools to read "
    "and edit a project's glossary, manage its meetings and tape sets, keep the "
    "story in project and meeting notes, start and watch pipeline runs (with "
    "explicit profile/backend/model/language), read the transcript text and list "
    "the artifacts a run produced. Recordings and model weights are local files; "
    "this server is a thin adapter over the same service the web console uses. "
    "For the glossary ↔ transcript tuning loop the agent orchestrates: read the "
    "transcript, refine the terms, then re-run with intent."
)


def _as_dict(obj: Any) -> dict[str, Any]:
    """A dataclass instance as a plain, JSON-serialisable dict."""
    return dataclasses.asdict(obj)


class ServiceTools:
    """One method per MCP tool; each is a thin ``clear_record.service`` call.

    Kept as an object (rather than module-level closures) so the tool surface is
    directly callable in tests and the registry/run-manager dependency is
    explicit. :func:`build_server` registers each method as a tool.
    """

    def __init__(self, registry: Registry, manager: RunManager | None = None) -> None:
        self.registry = registry
        self.manager = manager or RunManager(registry)

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
        if status is not None and status not in TERM_STATUSES:
            raise ToolError(
                f"status must be one of {list(TERM_STATUSES)}, got {status!r}"
            )
        return [
            _as_dict(term) for term in self.registry.list_terms(project, status=status)
        ]

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
    ) -> dict[str, Any]:
        """Start the pipeline for a meeting's latest tape set, with intent.

        The run executes in the background; read it with ``run_status`` and
        ``run_events``. Any of ``profile``, ``backend``, ``model``, ``language``
        and ``glossary`` (a glossary file path) may be set to re-run
        deliberately — e.g. after tuning the glossary; they are resolved by
        ``clear_record.core.options.resolve_options`` (explicit value →
        ``CR_*`` environment → profile → built-in default). Refused (with a
        reason) when the meeting has no workspace, no tape set, or a run
        already in flight.
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
            options = resolve_options(PipelineOptions(**explicit), profile=profile)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        try:
            run = self.manager.start(found, options)
        except ValueError as exc:
            raise ToolError(
                f"cannot start a run for {project}/{meeting}: {exc}"
            ) from exc
        return _as_dict(run)

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
        re-reading. Live state exists only for runs started by this server
        process; use ``run_status`` for a finished run's recorded status.
        """
        state: RunState | None = self.manager.state(run_id)
        if state is None:
            raise ToolError(
                f"no live events for run id {run_id}; "
                "use run_status for a run from a previous process"
            )
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
)


def build_server(
    registry: Registry,
    manager: RunManager | None = None,
    *,
    name: str = SERVER_NAME,
) -> MCPServer:
    """Build the MCP server over an opened registry (inject a temp one in tests)."""
    tools = ServiceTools(registry, manager)
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
