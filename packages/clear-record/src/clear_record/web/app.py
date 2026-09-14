"""The FastAPI app for the clear-record web console.

Two surfaces over the same **thin** service adapter:

- ``/api/*`` returns JSON — the machine surface the GUI, scripts and (later) the
  MCP server share. Every route is a small translation of a service call.
- ``/ui/*`` returns HTML fragments for the browser, driven by **htmx** (partial
  updates) and **Alpine.js** (local UI state). Server-rendered, no build step:
  the two libraries are vendored under ``static/`` so the console works offline.

No domain logic lives here, which is what lets the GUI, the MCP server and
scripts share one tested service seam.

The app binds localhost by default and has no authentication —
it is a local-first tool, not a hosted service (ADR-0013).
"""

from __future__ import annotations

import dataclasses
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from clear_record.service import (
    TERM_STATUSES,
    PipelineOptions,
    Registry,
    RunManager,
    RunState,
)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

#: The ASR backend ids the run form offers, in catalog order. A literal, not an
#: import of ``clear_record.providers``: the web layer may import only
#: ``core``/``service`` (layering guard), so it does not probe availability here.
BACKEND_CHOICES = ("apple", "nvidia", "amd")


class ProjectCreate(BaseModel):
    name: str
    notes: str = ""
    default_archive_root: str | None = None
    slug: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    default_archive_root: str | None = None


class TermCreate(BaseModel):
    term: str
    reading: str | None = None
    aliases: str | None = None
    definition: str | None = None
    status: str = "candidate"
    added_by: str = "human"
    notes: str | None = None


class TermUpdate(BaseModel):
    term: str | None = None
    reading: str | None = None
    aliases: str | None = None
    definition: str | None = None
    status: str | None = None
    notes: str | None = None


class MeetingCreate(BaseModel):
    title: str
    workspace_path: str
    recorded_at: str | None = None


class TapesUpdate(BaseModel):
    paths: list[str]


class RunCreate(BaseModel):
    backend: str = "apple"
    model: str | None = None
    language: str | None = None
    split: str = "auto"
    resume: bool = True
    jobs: int = 0


def _out(obj) -> dict:
    return dataclasses.asdict(obj)


def _run_context(state: RunState | None, *, meeting_id: int, fallback=None) -> dict:
    """The template context for one run fragment (live state, else the last row)."""
    if state is not None:
        context = state.summary()
        last = state.last
        context["meeting_id"] = state.meeting_id
        context["message"] = last.message if last else ""
        context["polling"] = state.status in ("queued", "running")
        return context
    return {
        "run_id": fallback.id,
        "meeting_id": meeting_id,
        "status": fallback.status,
        "stage": None,
        "index": 0,
        "total": 0,
        "eta_s": None,
        "message": "",
        "error": fallback.error,
        "polling": False,
    }


def create_app(registry: Registry, runs: RunManager | None = None) -> FastAPI:
    """Build the app around an opened registry (inject a temp one in tests).

    ``runs`` is the background run manager; inject one with a fake pipeline in
    tests so a whole run lifecycle is exercised with no ASR backend. It defaults
    to the real manager over the same registry.
    """
    runs = runs or RunManager(registry)
    app = FastAPI(
        title="clear-record",
        summary="Local project console: projects, glossary, meetings and runs.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # --- HTML views (htmx + Alpine) ---------------------------------------- #
    def project_rows() -> list[dict]:
        counts = registry.term_counts()
        return [
            {"project": project, "term_count": counts.get(project.slug, 0)}
            for project in registry.list_projects()
        ]

    def meeting_rows(slug: str) -> list[dict]:
        rows: list[dict] = []
        for meeting in registry.list_meetings(slug):
            tape_set = registry.latest_recording_set(meeting.id)
            latest = registry.list_runs(meeting.id)
            state = runs.state(latest[0].id) if latest else None
            if state is not None:
                run = _run_context(state, meeting_id=meeting.id)
            elif latest:
                run = _run_context(None, meeting_id=meeting.id, fallback=latest[0])
            else:
                run = None
            rows.append(
                {
                    "meeting": meeting,
                    "tapes": "\n".join(tape_set.paths) if tape_set else "",
                    "run": run,
                }
            )
        return rows

    def render_run(request: Request, state: RunState) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_run.html",
            {"run": _run_context(state, meeting_id=state.meeting_id)},
        )

    def render_run_error(
        request: Request, meeting_id: int, message: str
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_run.html",
            {
                "run": {
                    "run_id": None,
                    "meeting_id": meeting_id,
                    "status": "error",
                    "stage": None,
                    "index": 0,
                    "total": 0,
                    "eta_s": None,
                    "message": "",
                    "error": message,
                    "polling": False,
                }
            },
        )

    def detail(request: Request, slug: str) -> HTMLResponse:
        try:
            project = registry.require_project(slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        return TEMPLATES.TemplateResponse(
            request,
            "_detail.html",
            {
                "project": project,
                "terms": registry.list_terms(slug),
                "statuses": TERM_STATUSES,
                "meetings": meeting_rows(slug),
                "backends": BACKEND_CHOICES,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.get("/ui/projects", response_class=HTMLResponse)
    def ui_projects(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "_projects.html", {"projects": project_rows()}
        )

    @app.post("/ui/projects", response_class=HTMLResponse)
    def ui_create_project(request: Request, name: str = Form(...)) -> HTMLResponse:
        try:
            registry.create_project(name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request, "_projects.html", {"projects": project_rows()}
        )

    @app.get("/ui/projects/{slug}", response_class=HTMLResponse)
    def ui_project(request: Request, slug: str) -> HTMLResponse:
        return detail(request, slug)

    @app.post("/ui/projects/{slug}/glossary", response_class=HTMLResponse)
    def ui_add_term(
        request: Request,
        slug: str,
        term: str = Form(...),
        reading: str = Form(""),
        aliases: str = Form(""),
        definition: str = Form(""),
    ) -> HTMLResponse:
        try:
            registry.add_term(
                slug,
                term,
                reading=reading or None,
                aliases=aliases or None,
                definition=definition or None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return detail(request, slug)

    @app.post("/ui/glossary/{term_id}/status", response_class=HTMLResponse)
    def ui_set_status(
        request: Request, term_id: int, status: str = Form(...)
    ) -> HTMLResponse:
        try:
            term = registry.update_term(term_id, status=status)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no term {term_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return detail(request, term.project_slug)

    @app.delete("/ui/glossary/{term_id}", response_class=HTMLResponse)
    def ui_delete_term(request: Request, term_id: int) -> HTMLResponse:
        term = registry.get_term(term_id)
        if term is None:
            raise HTTPException(status_code=404, detail=f"no term {term_id}")
        registry.delete_term(term_id)
        return detail(request, term.project_slug)

    # --- HTML views: meetings and live runs --------------------------------- #
    @app.post("/ui/projects/{slug}/meetings", response_class=HTMLResponse)
    def ui_create_meeting(
        request: Request,
        slug: str,
        title: str = Form(...),
        workspace_path: str = Form(""),
        recorded_at: str = Form(""),
    ) -> HTMLResponse:
        try:
            registry.create_meeting(
                slug,
                title,
                workspace_path=workspace_path or None,
                recorded_at=recorded_at or None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return detail(request, slug)

    @app.post("/ui/meetings/{meeting_id}/tapes", response_class=HTMLResponse)
    def ui_set_tapes(
        request: Request, meeting_id: int, paths: str = Form("")
    ) -> HTMLResponse:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        tapes = [line.strip() for line in paths.splitlines() if line.strip()]
        try:
            registry.set_recording_set(meeting_id, tapes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return detail(request, meeting.project_slug)

    @app.post("/ui/meetings/{meeting_id}/runs", response_class=HTMLResponse)
    def ui_start_run(
        request: Request,
        meeting_id: int,
        backend: str = Form("apple"),
        model: str = Form(""),
        language: str = Form(""),
    ) -> HTMLResponse:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        options = PipelineOptions(
            backend=backend, model=model or None, language=language or None
        )
        try:
            run = runs.start(meeting, options)
        except ValueError as exc:
            # A conflict while a run is live: re-render the live fragment so its
            # polling is not torn down by an error response (htmx skips 4xx swaps).
            active = runs.active_state(meeting_id)
            if active is not None:
                return render_run(request, active)
            return render_run_error(request, meeting_id, str(exc))
        return render_run(request, runs.require_state(run.id))

    @app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    def ui_run(request: Request, run_id: int) -> HTMLResponse:
        try:
            state = runs.require_state(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no run {run_id}") from exc
        return render_run(request, state)

    # --- JSON API (machines, scripts, later MCP) ---------------------------- #
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "registry": str(registry.db_path)}

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        counts = registry.term_counts()
        projects = []
        for project in registry.list_projects():
            row = _out(project)
            row["term_count"] = counts.get(project.slug, 0)
            projects.append(row)
        return projects

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict:
        try:
            project = registry.create_project(
                body.name,
                notes=body.notes,
                default_archive_root=body.default_archive_root,
                slug=body.slug,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _out(project)

    @app.get("/api/projects/{slug}")
    def get_project(slug: str) -> dict:
        try:
            return _out(registry.require_project(slug))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc

    @app.patch("/api/projects/{slug}")
    def update_project(slug: str, body: ProjectUpdate) -> dict:
        try:
            project = registry.update_project(
                slug,
                name=body.name,
                notes=body.notes,
                default_archive_root=body.default_archive_root,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _out(project)

    @app.get("/api/projects/{slug}/glossary")
    def list_terms(slug: str, status: str | None = None) -> list[dict]:
        try:
            registry.require_project(slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        return [_out(term) for term in registry.list_terms(slug, status=status)]

    @app.post("/api/projects/{slug}/glossary", status_code=201)
    def add_term(slug: str, body: TermCreate) -> dict:
        try:
            term = registry.add_term(
                slug,
                body.term,
                reading=body.reading,
                aliases=body.aliases,
                definition=body.definition,
                status=body.status,
                added_by=body.added_by,
                notes=body.notes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _out(term)

    @app.patch("/api/glossary/{term_id}")
    def update_term(term_id: int, body: TermUpdate) -> dict:
        try:
            term = registry.update_term(
                term_id,
                term=body.term,
                reading=body.reading,
                aliases=body.aliases,
                definition=body.definition,
                status=body.status,
                notes=body.notes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no term {term_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _out(term)

    @app.delete("/api/glossary/{term_id}", status_code=204)
    def delete_term(term_id: int) -> None:
        try:
            registry.delete_term(term_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no term {term_id}") from exc

    # --- JSON API: meetings, tapes and runs --------------------------------- #
    @app.post("/api/projects/{slug}/meetings", status_code=201)
    def create_meeting(slug: str, body: MeetingCreate) -> dict:
        try:
            meeting = registry.create_meeting(
                slug,
                body.title,
                recorded_at=body.recorded_at,
                workspace_path=body.workspace_path,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _out(meeting)

    @app.get("/api/projects/{slug}/meetings")
    def list_meetings(slug: str) -> list[dict]:
        try:
            registry.require_project(slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        return [_out(meeting) for meeting in registry.list_meetings(slug)]

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting(meeting_id: int) -> dict:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return _out(meeting)

    @app.put("/api/meetings/{meeting_id}/tapes", status_code=201)
    def set_tapes(meeting_id: int, body: TapesUpdate) -> dict:
        try:
            tape_set = registry.set_recording_set(meeting_id, body.paths)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"no meeting {meeting_id}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _out(tape_set)

    @app.post("/api/meetings/{meeting_id}/runs", status_code=202)
    def start_run(meeting_id: int, body: RunCreate) -> dict:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        if runs.active_state(meeting_id) is not None:
            raise HTTPException(
                status_code=409, detail="a run is already in flight for this meeting"
            )
        options = PipelineOptions(
            backend=body.backend,
            model=body.model,
            language=body.language,
            split=body.split,
            resume=body.resume,
            jobs=body.jobs,
        )
        try:
            run = runs.start(meeting, options)
        except ValueError as exc:
            # No workspace or no tape set: a bad request, not a conflict.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run": _out(run),
            "state": runs.require_state(run.id).summary(),
        }

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict:
        run = registry.get_run(run_id)
        state = runs.state(run_id)
        if run is None or state is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return {"run": _out(run), "state": state.summary()}

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: int, after: int = 0) -> dict:
        try:
            state = runs.require_state(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no run {run_id}") from exc
        events = state.events_since(after)
        return {
            "events": [_out(event) for event in events],
            "next": after + len(events),
        }

    @app.post("/api/shutdown", status_code=202)
    def shutdown() -> dict:
        """Ask the managed server to stop (the desktop app's Quit button).

        The console runs as a local server; a windowed desktop build has no
        terminal to Ctrl-C, so the UI needs an explicit way to stop it. When the
        app is served by something other than this module's ``serve()`` (e.g. a
        test client), there is nothing to stop.
        """
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(
                status_code=409, detail="not running under the managed server"
            )
        server.should_exit = True
        return {"status": "stopping"}

    return app


def serve(
    *,
    host: str,
    port: int,
    open_browser: bool,
    data_dir: str | None = None,
) -> int:
    """Run the console; called by the ``clear-record web`` handler."""
    import uvicorn

    app = create_app(Registry.open(data_dir=data_dir))
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # Exposed so `POST /api/shutdown` can ask the server to stop — the desktop
    # build has no terminal to interrupt.
    app.state.server = server
    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    server.run()
    return 0


__all__ = ["create_app", "serve"]
