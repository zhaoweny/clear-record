"""The FastAPI app for the clear-record web console.

Two surfaces over the same **thin** service adapter:

- ``/api/*`` returns JSON — the machine surface the GUI, scripts and (later) the
  MCP server share. Every route is a small translation of a service call.
- ``/ui/*`` returns HTML fragments for the browser, driven by **htmx** (partial
  updates) and **Alpine.js** (local UI state). Server-rendered: the assets are
  **built** from ``frontend/`` (Tailwind v4 + Vite) and the **compiled output is
  committed** under ``static/``, so the console works offline and a plain install
  needs no Node (ADR-0023).

No domain logic lives here, which is what lets the GUI, the MCP server and
scripts share one tested service seam.

The app binds localhost by default and has no authentication —
it is a local-first tool, not a hosted service (ADR-0013). "Localhost-only"
bounds who can connect, not who can act: the :mod:`clear_record.web.guard`
middleware rejects a hostile page's cross-origin or rebound requests (ADR-0021),
while remote access stays the operator's reverse proxy.
"""

from __future__ import annotations

import dataclasses
import shutil
import threading
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from clear_record.core import (
    PROFILE_CUSTOM,
    PROFILES,
    profile_values,
    resolve_options,
)
from clear_record.core import i18n
from clear_record.core.i18n import install_if_unset, tr, trn
from clear_record.service import (
    BUNDLE_FILENAME,
    TERM_STATUSES,
    PipelineOptions,
    Registry,
    RunManager,
    RunState,
    archive_meeting,
    collect_bundle,
    managed,
    verify_archive,
)
from clear_record.web import guard

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

#: One lookup point: the same ``tr``/``trn`` a Python module imports, exposed to
#: Jinja, so a template and a module cannot diverge. They read whatever catalog
#: the process installed — the request's own cookie/``Accept-Language`` choice
#: (see :func:`resolve_web_locale` and the middleware in :func:`create_app`), or
#: the startup ``CR_LANG``/``LANG`` default. With none installed they return the
#: English source verbatim.
TEMPLATES.env.globals["tr"] = tr
TEMPLATES.env.globals["trn"] = trn

#: The cookie that persists the console's explicit language choice: the one new
#: piece of state the switcher adds, and it carries a language tag and **nothing
#: else** — no session, no identity.
LANG_COOKIE = "cr_lang"

#: The console's language choices as ``(catalog tag, endonym)``. The names are
#: deliberately **not** run through ``tr``: a language picker that renamed a
#: language would stop being legible to the people who read it, so each option
#: stays in its own language. ``en`` is the source locale (no catalog) and is
#: therefore always offered.
LANGUAGE_CHOICES = (("en", "English"), ("zh_CN", "简体中文"))
TEMPLATES.env.globals["language_choices"] = LANGUAGE_CHOICES

#: The browser file-picker's ``accept`` filter, built from the service's own
#: audio allow-list (``managed.AUDIO_SUFFIXES`` — the exact list the upload guard
#: checks) rather than restated here, so the picker and the guard cannot drift.
#: The web layer may not import ``clear_record.cli.workspace`` directly (the
#: layering guard), but ``managed`` already owns that list for uploads.
AUDIO_ACCEPT = ",".join(sorted(managed.AUDIO_SUFFIXES))
TEMPLATES.env.globals["audio_accept"] = AUDIO_ACCEPT


def _accept_language_tags(header: str | None) -> list[str]:
    """The language tags in an ``Accept-Language`` header, highest ``q`` first.

    ``q=0`` means "not acceptable" and is dropped; ties keep the header's order.
    Malformed quantities are treated as ``0`` rather than raising — the header
    comes from the network and must not be able to break a page render.
    """
    if not header:
        return []
    ranked: list[tuple[float, int, str]] = []
    for position, item in enumerate(header.split(",")):
        item = item.strip()
        if not item:
            continue
        tag, _, params = item.partition(";")
        quality = 1.0
        for param in params.split(";"):
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            ranked.append((-quality, position, tag.strip()))
    ranked.sort()
    return [tag for _, _, tag in ranked]


def _shipped_locale(tag: str | None) -> str | None:
    """The shipped locale ``tag`` names, or ``None`` when nothing matches.

    Handles the browser spellings a region/language may arrive as: ``zh-CN`` and
    ``zh`` both resolve to the ``zh_CN`` catalog, and ``en``/``en-US`` to the
    source locale. Matching ignores case and the ``-``/``_`` separator.
    """
    code = i18n.normalize_locale(tag)
    if code is None:
        return None
    base = code.lower().replace("-", "_")
    shipped = i18n.available_locales()
    for name in shipped:
        if name.lower() == base:
            return name
    language = base.split("_")[0]
    if language == i18n.SOURCE_LOCALE:
        return i18n.SOURCE_LOCALE
    return next(
        (name for name in shipped if name.lower().split("_")[0] == language), None
    )


def resolve_web_locale(
    cookie: str | None,
    accept_language: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The console's locale: cookie > ``Accept-Language`` > ``CR_LANG`` > env > English.

    The CLI chain (``--lang`` > ``CR_LANG`` > ``LC_ALL`` > ``LANG`` > English)
    stays on :func:`clear_record.core.i18n.resolve_locale`; the web adds the two
    request-borne layers on top and delegates the environment tail to it. A
    candidate that names no shipped catalog is skipped (a stale cookie or an
    unsupported browser preference falls through), never guessed.
    """
    for candidate in (cookie, *_accept_language_tags(accept_language)):
        if candidate == "*":
            continue
        shipped = _shipped_locale(candidate)
        if shipped is not None:
            return shipped
    return i18n.resolve_locale(environ=environ)


#: The ASR backend ids the run form offers, in catalog order. A literal, not an
#: import of ``clear_record.providers``: the web layer may import only
#: ``core``/``service`` (layering guard), so it does not probe availability here.
BACKEND_CHOICES = ("apple", "nvidia", "amd")

#: The picker's choices, derived from the shared ``core`` table (never restated),
#: with ``custom`` — "no preset" — last. A profile added to ``PROFILES`` shows up
#: in the picker with no web change, so the console cannot drift from the CLI.
PROFILE_CHOICES = tuple(name for name in PROFILES if name != PROFILE_CUSTOM) + (
    PROFILE_CUSTOM,
)


def profile_preview(profile: str) -> dict:
    """The knob values a profile resolves to, via the shared resolver.

    ``profile_values`` supplies the knobs the preset touches and
    :func:`resolve_options` their resolved values, so the preview is the table
    read through the same **explicit > ``CR_*`` env > profile > default**
    precedence the run will use. ``custom`` touches no knob, so it previews as
    "no preset". An unknown profile raises :class:`ValueError` from ``core``.
    """
    resolved = resolve_options(PipelineOptions(), profile=profile)
    return {
        "profile": resolved.profile,
        "custom": resolved.profile == PROFILE_CUSTOM,
        "knobs": {
            name: getattr(resolved, name)
            for name in profile_values(resolved.profile)
            if getattr(resolved, name) is not None
        },
    }


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
    #: A user-chosen workspace (ADR-0007), kept for the CLI-shaped flow. An
    #: upload requires a *managed* workspace; see ``managed=True``.
    workspace_path: str | None = None
    recorded_at: str | None = None
    #: Provision an app-owned workspace under ``CR_WORKSPACE_ROOT`` (ADR-0024)
    #: instead of using ``workspace_path``.
    managed: bool = False


class TapesUpdate(BaseModel):
    paths: list[str]


class RunCreate(BaseModel):
    backend: str = "apple"
    model: str | None = None
    language: str | None = None
    split: str = "auto"
    resume: bool = True
    jobs: int = 0
    profile: str = PROFILE_CUSTOM


class ArchiveCreate(BaseModel):
    root: str | None = None


def _out(obj) -> dict:
    return dataclasses.asdict(obj)


def _content_length(request: Request) -> int | None:
    """The declared body size, or ``None`` when the client did not say.

    It is an upper bound on the uploaded file (it includes multipart framing),
    which is enough for the pre-transfer size and disk-space guards.
    """
    raw = request.headers.get("content-length")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


#: HTTP status for each upload guard. Kept in the web layer so the service stays
#: free of HTTP notions; the message itself is always the service's.
_UPLOAD_STATUS = {
    managed.UploadTooLarge: 413,
    managed.DisallowedExtension: 415,
    managed.InsufficientSpace: 507,
}


def _upload_status(exc: managed.UploadRejected) -> int:
    for kind, status in _UPLOAD_STATUS.items():
        if isinstance(exc, kind):
            return status
    return 400


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


def create_app(
    registry: Registry,
    runs: RunManager | None = None,
    *,
    trusted_hosts: Sequence[str] | None = None,
) -> FastAPI:
    """Build the app around an opened registry (inject a temp one in tests).

    ``runs`` is the background run manager; inject one with a fake pipeline in
    tests so a whole run lifecycle is exercised with no ASR backend. It defaults
    to the real manager over the same registry.

    ``trusted_hosts`` overrides the extra hostnames the request guard accepts
    (default: ``CR_TRUSTED_HOSTS``, on top of loopback); tests and embedders can
    pass an explicit set, and ``()`` pins the loopback-only default.
    """
    # The console's user-facing text is translated per process. A CLI ``--lang``
    # (or an explicit ``install``) has already chosen; otherwise honour
    # ``CR_LANG``/``LANG``. With neither, the catalog stays null and the English
    # source renders — the byte-identical default.
    install_if_unset()
    runs = runs or RunManager(registry)
    app = FastAPI(
        title="clear-record",
        summary="Local project console: projects, glossary, meetings and runs.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    extra_hosts = (
        guard.normalize_hosts(trusted_hosts)
        if trusted_hosts is not None
        else guard.trusted_extra_hosts()
    )

    @app.middleware("http")
    async def request_locale(request: Request, call_next):
        """Pick this request's locale and install its catalog.

        A cookie or an ``Accept-Language`` header is a preference the *request*
        carries, so its catalog is installed here (cookie > header > the
        environment tail). With neither, the catalog chosen at startup
        (``CR_LANG``/``LANG``, an explicit ``install``, or a test's ``use``) is
        left untouched — which is also what keeps the pseudo-locale boundary
        tests honest.

        ``gettext``'s catalog is process-wide, not per-request; that is fine for
        a local single-user console (ADR-0013) whose browser sends
        ``Accept-Language`` on every request, and it is the same global seam
        ``install_if_unset`` already used.
        """
        cookie = request.cookies.get(LANG_COOKIE)
        accept_language = request.headers.get("accept-language")
        if cookie is not None or accept_language is not None:
            locale = resolve_web_locale(cookie, accept_language)
            i18n.install(locale)
        else:
            locale = i18n.current_locale() or i18n.SOURCE_LOCALE
        request.state.locale = locale
        return await call_next(request)

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        """Reject a hostile page's rebound or cross-origin requests (ADR-0021).

        ``Host`` is checked on every request (DNS rebinding is method-blind and a
        rebound read is still a disclosure); ``Origin``/``Referer`` on the
        state-changing ones. A rejection is a plain 403 with an actionable
        message — it is the attacker's request, not the operator's UI.
        """
        problem = guard.host_problem(request.headers.get("host"), extra_hosts)
        if problem is None and request.method not in guard.SAFE_METHODS:
            problem = guard.source_problem(
                request.headers.get("origin"),
                request.headers.get("referer"),
                extra_hosts,
            )
        if problem is not None:
            return JSONResponse(status_code=403, content={"detail": problem})
        return await call_next(request)

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
                    "managed": managed.is_managed(meeting),
                    "tapes": "\n".join(tape_set.paths) if tape_set else "",
                    "run": run,
                }
            )
        return rows

    def archive_status(archive) -> dict | None:
        """The verification summary, or None when the manifest is gone."""
        try:
            return verify_archive(archive.root_path)
        except FileNotFoundError:
            return None

    def archive_rows(slug: str) -> list[dict]:
        rows: list[dict] = []
        for meeting in registry.list_meetings(slug):
            for archive in registry.list_archives(meeting.id):
                rows.append({"archive": archive, "meeting": meeting})
        rows.sort(key=lambda row: row["archive"].id, reverse=True)
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

    def detail(request: Request, slug: str, error: str | None = None) -> HTMLResponse:
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
                "profiles": PROFILE_CHOICES,
                "profile_default": PROFILE_CUSTOM,
                "profile_options": profile_preview(PROFILE_CUSTOM),
                "archives": archive_rows(slug),
                "error": error,
            },
        )

    # --- HTML views: a meeting's managed storage (ADR-0024) ---------------- #
    def human_bytes(count: int) -> str:
        """Format a byte count for the storage panel.

        Display only — every number is the service's ``meeting_storage``
        accounting; this just renders it for a human. (The service has its own
        formatter for the guard messages; the web layer keeps a display copy so
        the service surface stays untouched.)
        """
        value = float(count)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{count} B"  # pragma: no cover - unreachable

    def upload_error_label(exc: managed.UploadRejected) -> str:
        """A short, translated label for one upload guard.

        The *reason* is always the service's own message, rendered beside this,
        so the web layer never restates a guard's rule or its facts.
        """
        if isinstance(exc, managed.UnsafeFilename):
            return tr("Filename refused")
        if isinstance(exc, managed.DisallowedExtension):
            return tr("Not an audio tape")
        if isinstance(exc, managed.UploadTooLarge):
            return tr("Upload too large")
        if isinstance(exc, managed.InsufficientSpace):
            return tr("Not enough disk space")
        return tr("Upload refused")

    def refusal(exc: managed.UploadRejected, label: str) -> dict:
        """A guard failure as the panel shows it: a translated label + the
        service's message. The message goes through ``tr`` like every other
        user-facing string; it is composed at run time (a filename, a size), so
        it is not itself a catalog message ID yet — see the report's follow-up.
        """
        return {"label": label, "reason": tr(str(exc))}

    def upload_refusal(meeting) -> dict | None:
        """Why this meeting cannot take an upload, in the service's own words.

        A user-chosen workspace has no managed place to upload to (ADR-0007).
        Rather than restate the rule, ask the service's own guard: with
        ``declared_bytes=0`` the size and disk guards cannot fire, leaving only
        the workspace guard to speak. A meeting with no workspace at all gets
        one provisioned on first upload, so it is not refused.
        """
        if managed.is_managed(meeting) or not meeting.workspace_path:
            return None
        try:
            managed.precheck_upload(registry, meeting, declared_bytes=0)
        except managed.UploadRejected as exc:
            return refusal(exc, tr("This meeting cannot take an upload"))
        return None  # pragma: no cover - is_managed / no-path are handled above

    def storage_context(meeting) -> dict:
        """The template context for one meeting's storage panel (ADR-0024).

        Everything factual is the service's: the resolved workspace path, the
        workspace and tape sizes, the managed root and the tapes. Free space on
        the managed root is the one number the service reports only inside a
        guard message, so the view reads it with the same stdlib call the guard
        uses (a follow-up note asks the service to expose it — see the report).
        """
        usage = managed.meeting_storage(registry, meeting)
        free_bytes: int | None = None
        if usage["managed"]:
            try:
                free_bytes = shutil.disk_usage(Path(usage["managed_root"])).free
            except OSError:  # pragma: no cover - a root that vanished
                free_bytes = None
        tapes = [
            {
                "id": tape["id"],
                "name": tape["name"],
                "path": tape["path"],
                "sha256": tape["sha256"],
                "sha256_short": tape["sha256"][:12],
                "bytes": tape["bytes"],
                "size": human_bytes(tape["bytes"]),
            }
            for tape in usage["tapes"]
        ]
        tapes_bytes = sum(tape["bytes"] for tape in tapes)
        return {
            "meeting_id": meeting.id,
            "workspace_path": usage["workspace_path"],
            "managed": usage["managed"],
            # A managed meeting uploads; so does one with no workspace yet (the
            # first upload provisions a managed one). A user-chosen one cannot.
            "can_upload": usage["managed"] or not usage["workspace_path"],
            "managed_root": usage["managed_root"],
            "meeting_bytes": usage["bytes"],
            "meeting_size": human_bytes(usage["bytes"]),
            "tapes_bytes": tapes_bytes,
            "tapes_size": human_bytes(tapes_bytes),
            "free_bytes": free_bytes,
            "free_size": None if free_bytes is None else human_bytes(free_bytes),
            "tapes": tapes,
            "upload_refusal": upload_refusal(meeting),
        }

    def render_storage(
        request: Request, meeting, error: dict | None = None
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_storage.html",
            {"storage": storage_context(meeting), "error": error},
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "index.html", {})

    @app.post("/ui/language")
    def ui_set_language(request: Request, lang: str = Form(...)) -> RedirectResponse:
        """Persist the console's explicit language choice, then reload.

        The cookie is the whole state: a language tag, no session, no identity.
        An unknown value sets nothing rather than being stored and guessed at
        later; the redirect is always to the console root, so no request input
        ever becomes a redirect target.
        """
        response = RedirectResponse("/", status_code=303)
        chosen = _shipped_locale(lang)
        if chosen is not None:
            response.set_cookie(
                LANG_COOKIE,
                chosen,
                max_age=60 * 60 * 24 * 365,
                path="/",
                httponly=True,
                samesite="lax",
            )
        return response

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
        """Create a meeting, managed by default (owner, 2026-09-15).

        A blank path is the common case: the app provisions a managed workspace
        under the root. A path is the override — a user-chosen workspace, kept
        exactly as before (ADR-0007), and it can hold no uploads.
        """
        try:
            meeting = registry.create_meeting(
                slug,
                title,
                workspace_path=workspace_path or None,
                recorded_at=recorded_at or None,
            )
            if not workspace_path:
                managed.ensure_managed_workspace(registry, meeting)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except managed.UploadRejected as exc:
            # The meeting exists but its managed workspace could not be made:
            # re-render the project with the service's message.
            return detail(request, slug, error=str(exc))
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

    @app.get("/ui/meetings/{meeting_id}/storage", response_class=HTMLResponse)
    def ui_meeting_storage(request: Request, meeting_id: int) -> HTMLResponse:
        """One meeting's storage panel: resolved path, sizes, tapes, controls."""
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return render_storage(request, meeting)

    @app.post("/ui/meetings/{meeting_id}/tapes/upload", response_class=HTMLResponse)
    async def ui_upload_tape(request: Request, meeting_id: int) -> HTMLResponse:
        """Receive one tape and re-render the panel (ADR-0024).

        The service's guards are the same ones the JSON endpoint uses. A refusal
        re-renders the panel with the guard's translated label and its own
        message as a 200, so htmx swaps the error into place and the form stays
        usable. The body is one stream: the endpoint takes no upload id, so
        there is no resume to offer (see the report).
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        declared = _content_length(request)
        try:
            meeting = managed.precheck_upload(registry, meeting, declared)
            try:
                form = await request.form()
            except Exception as exc:  # noqa: BLE001 - a malformed body is a refusal
                raise managed.UploadRejected(
                    tr("could not read the multipart upload: {error}", error=exc)
                ) from exc
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise managed.UploadRejected(
                    tr("attach the tape as a multipart file part named 'file'")
                )
            await run_in_threadpool(
                managed.upload_tape,
                registry,
                meeting,
                upload.file,
                filename=upload.filename,
                declared_bytes=declared,
            )
        except managed.UploadRejected as exc:
            return render_storage(
                request, meeting, error=refusal(exc, upload_error_label(exc))
            )
        return render_storage(request, meeting)

    @app.delete(
        "/ui/meetings/{meeting_id}/tapes/{tape_id}", response_class=HTMLResponse
    )
    def ui_delete_tape(request: Request, meeting_id: int, tape_id: int) -> HTMLResponse:
        """Delete one managed tape, re-rendering the panel."""
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            managed.delete_tape(registry, meeting, tape_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"no tape {tape_id} for meeting {meeting_id}"
            ) from exc
        except managed.UploadRejected as exc:
            return render_storage(
                request, meeting, error=refusal(exc, tr("Delete refused"))
            )
        return render_storage(request, meeting)

    @app.delete("/ui/meetings/{meeting_id}/tapes", response_class=HTMLResponse)
    def ui_delete_meeting_tapes(request: Request, meeting_id: int) -> HTMLResponse:
        """Delete every uploaded tape of the meeting (the per-meeting control).

        Still manual and still confirmed: the archive is the durable copy, and
        nothing here deletes on the node's own initiative (owner, 2026-09-15).
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            for tape in registry.list_tapes(meeting_id):
                managed.delete_tape(registry, meeting, tape.id)
        except managed.UploadRejected as exc:
            return render_storage(
                request, meeting, error=refusal(exc, tr("Delete refused"))
            )
        return render_storage(request, meeting)

    @app.post("/ui/meetings/{meeting_id}/archives", response_class=HTMLResponse)
    def ui_archive_meeting(
        request: Request, meeting_id: int, root: str = Form("")
    ) -> HTMLResponse:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            archive_meeting(registry, meeting, root or None)
        except ValueError as exc:
            # A missing archive root is fixable in the form, so re-render the
            # project with the message rather than an error status htmx skips.
            return detail(request, meeting.project_slug, error=str(exc))
        return detail(request, meeting.project_slug)

    @app.get("/ui/archives/{archive_id}/verify", response_class=HTMLResponse)
    @app.post("/ui/archives/{archive_id}/verify", response_class=HTMLResponse)
    def ui_verify_archive(request: Request, archive_id: int) -> HTMLResponse:
        archive = registry.get_archive(archive_id)
        if archive is None:
            raise HTTPException(status_code=404, detail=f"no archive {archive_id}")
        row = {"archive": archive, "verification": archive_status(archive)}
        return TEMPLATES.TemplateResponse(request, "_archive_status.html", {"row": row})

    @app.get("/ui/profile-options", response_class=HTMLResponse)
    def ui_profile_options(
        request: Request, profile: str = PROFILE_CUSTOM
    ) -> HTMLResponse:
        """The resolved-knobs fragment for the profile currently chosen.

        Fetched by the picker's ``hx-get`` on change; the resolver — not the
        template — decides the values, so the preview cannot disagree with the run.
        """
        try:
            preview = profile_preview(profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request, "_profile_options.html", {"profile_options": preview}
        )

    @app.post("/ui/meetings/{meeting_id}/runs", response_class=HTMLResponse)
    def ui_start_run(
        request: Request,
        meeting_id: int,
        backend: str = Form("apple"),
        model: str = Form(""),
        language: str = Form(""),
        profile: str = Form(PROFILE_CUSTOM),
    ) -> HTMLResponse:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            options = resolve_options(
                PipelineOptions(
                    backend=backend, model=model or None, language=language or None
                ),
                profile=profile,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @app.get("/ui/diagnostics")
    def ui_diagnostics() -> Response:
        """The same redacted bundle `clear-record diagnose` writes, as a download.

        A plain link target (``<a download>``): a file download needs no JS. The
        bundle states what it withholds and that nothing is transmitted.
        """
        bundle = collect_bundle(registry=registry)
        return Response(
            bundle,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{BUNDLE_FILENAME}"'
            },
        )

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
                workspace_path=None if body.managed else body.workspace_path,
            )
            if body.managed:
                meeting = managed.ensure_managed_workspace(registry, meeting)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        except managed.UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @app.post("/api/meetings/{meeting_id}/tapes", status_code=201)
    async def upload_tape(request: Request, meeting_id: int) -> dict:
        """Receive one tape into the meeting's **managed** workspace (ADR-0024).

        Multipart, one file part named ``file``. The guards run first — and the
        size/disk guards run against ``Content-Length`` **before** the body is
        read, so an over-cap or no-space upload is refused without the transfer.
        The bytes then stream to a ``.part`` file and are renamed into place;
        only a complete, checksummed tape is recorded.

        A single POST, deliberately: a dropped multi-GB upload restarts. Chunked
        / resumable upload is out of scope for this slice.
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        declared = _content_length(request)
        try:
            meeting = managed.precheck_upload(registry, meeting, declared)
        except managed.UploadRejected as exc:
            raise HTTPException(
                status_code=_upload_status(exc), detail=str(exc)
            ) from exc

        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - a malformed body is a 400
            raise HTTPException(
                status_code=400,
                detail=f"could not read the multipart upload: {exc}",
            ) from exc
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise HTTPException(
                status_code=400,
                detail="attach the tape as a multipart file part named 'file'",
            )
        try:
            tape = await run_in_threadpool(
                managed.upload_tape,
                registry,
                meeting,
                upload.file,
                filename=upload.filename,
                declared_bytes=declared,
            )
        except managed.UploadRejected as exc:
            raise HTTPException(
                status_code=_upload_status(exc), detail=str(exc)
            ) from exc
        return _out(tape)

    @app.get("/api/meetings/{meeting_id}/storage")
    def meeting_storage(meeting_id: int) -> dict:
        """A managed meeting's workspace size and uploaded tapes (ADR-0024)."""
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return managed.meeting_storage(registry, meeting)

    @app.delete("/api/meetings/{meeting_id}/tapes/{tape_id}")
    def delete_tape(meeting_id: int, tape_id: int) -> dict:
        """Delete one managed tape's file and record.

        The archive is the durable copy; the response says so, and a tape in a
        user-chosen workspace is refused (it is not app-owned data).
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            tape = managed.delete_tape(registry, meeting, tape_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"no tape {tape_id} for meeting {meeting_id}"
            ) from exc
        except managed.UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "deleted": _out(tape),
            "note": (
                "the archive is the durable copy; archive this meeting before "
                "deleting its tapes if you need to keep it"
            ),
        }

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
            options = resolve_options(options, profile=body.profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    # --- JSON API: archives ------------------------------------------------- #
    @app.post("/api/meetings/{meeting_id}/archives", status_code=201)
    def post_archive(meeting_id: int, body: ArchiveCreate | None = None) -> dict:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        root = body.root if body else None
        try:
            archive = archive_meeting(registry, meeting, root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _out(archive)

    @app.get("/api/projects/{slug}/archives")
    def list_project_archives(slug: str) -> list[dict]:
        try:
            registry.require_project(slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        archives = [
            archive
            for meeting in registry.list_meetings(slug)
            for archive in registry.list_archives(meeting.id)
        ]
        archives.sort(key=lambda archive: archive.id, reverse=True)
        return [_out(archive) for archive in archives]

    @app.get("/api/meetings/{meeting_id}/archives")
    def list_meeting_archives(meeting_id: int) -> list[dict]:
        if registry.meeting_by_id(meeting_id) is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return [_out(archive) for archive in registry.list_archives(meeting_id)]

    @app.post("/api/archives/{archive_id}/verify")
    def post_verify(archive_id: int) -> dict:
        archive = registry.get_archive(archive_id)
        if archive is None:
            raise HTTPException(status_code=404, detail=f"no archive {archive_id}")
        try:
            return verify_archive(archive.root_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    trusted_hosts: Sequence[str] | None = None,
) -> int:
    """Run the console; called by the ``clear-record web`` handler.

    ``trusted_hosts`` is forwarded to :func:`create_app` so ``web --tailscale``
    can trust the resolved tailnet name in-process, with no environment variable
    handed to a child (the design decision in ticket 01). ``None`` keeps the
    ``CR_TRUSTED_HOSTS`` default.
    """
    import uvicorn

    app = create_app(Registry.open(data_dir=data_dir), trusted_hosts=trusted_hosts)
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
