"""The FastAPI app for the clear-record web console.

A **thin** adapter over :mod:`clear_record.service`: every route is a small
translation of a service call into HTTP/JSON. No domain logic lives here, which
is what lets the GUI, the MCP server and scripts share one tested service seam.

The app binds localhost by default and has no authentication —
it is a local-first tool, not a hosted service (ADR-0013).
"""

from __future__ import annotations

import dataclasses
import threading
import webbrowser

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from clear_record.service import Registry
from clear_record.web.assets import INDEX_HTML


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

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "registry": str(registry.db_path)}

    # --- projects ---------------------------------------------------------- #
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

    # --- glossary ---------------------------------------------------------- #
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
    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


__all__ = ["create_app", "serve"]
