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

from clear_record.service import TERM_STATUSES, Registry

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


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


def _out(obj) -> dict:
    return dataclasses.asdict(obj)


def create_app(registry: Registry) -> FastAPI:
    """Build the app around an opened registry (inject a temp one in tests)."""
    app = FastAPI(
        title="clear-record",
        summary="Local project console: projects, glossary, and (soon) runs.",
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
