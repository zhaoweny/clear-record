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
from clear_record.core.i18n import deferred, install_if_unset, tr, trn
from clear_record.service import (
    BACKEND_AUTO,
    BUNDLE_FILENAME,
    TASK_KINDS,
    TERM_STATUSES,
    AgentConfig,
    AgentTaskError,
    MeetingAgent,
    ModelNotOnDisk,
    NoBackendAvailable,
    PipelineOptions,
    Registry,
    Runner,
    RunManager,
    RunState,
    archive_meeting,
    collect_bundle,
    default_config,
    describe_draft,
    managed,
    read_transcript,
    resolve_run,
    verify_archive,
)
from clear_record.service.auto import render_message as render_service_message
from clear_record.service.setup import (
    DEFAULT_SMALL_MODEL,
    Detection,
    PI_AGENT,
    SetupError,
    detect,
    find_harness,
    mcp_server_entry,
    pull_model,
    remember_harness,
    resolve_harness,
    setup_view,
    verify_endpoint,
    write_agent_settings,
    write_mcp_config,
)
from clear_record.service.webhooks import (
    WebhookEmitter,
    WebhookStatus,
    default_emitter,
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

#: The Settings page's sections (ADR-0027): each is a real URL,
#: ``/settings/<slug>``. This is the control plane's spine, not the project
#: navigation. Ticket 03 grows the set; today it holds the two surfaces the
#: console already has - the agent endpoint/MCP setup and the webhook panel.
#: The labels are ``deferred`` so Babel extracts them here while the template's
#: ``tr`` picks the *request's* locale, not the import-time one.
SETTINGS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("agent", deferred("Agent")),
    ("webhooks", deferred("Webhooks")),
)

#: How many of the newest meetings the Projects landing's activity line and a
#: project's Overview show. Both are cheap registry reads; walking every
#: workspace for tapes and transcripts is the project's Media tab's job.
RECENT_MEETINGS = 5


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


#: The ASR backend ids the run form offers, in catalog order, with the
#: capability-driven ``auto`` sentinel last (never the default). A literal, not
#: an import of ``clear_record.providers``: the web layer may import only
#: ``core``/``service`` (layering guard), so it does not probe availability here
#: — ``service.auto`` resolves ``auto`` at submit time.
BACKEND_CHOICES = ("apple", "nvidia", "amd", BACKEND_AUTO)

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
    #: Opt-in: resolve the profile/model (and per-speaker attribution) from the
    #: machine and the tape, and record the CLI's explanation with the run.
    #: Never the default — a run is unchanged unless the caller asks.
    auto: bool = False


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
    managed.InvalidUploadId: 400,
    # The node does not implement resuming, so an id whose scratch file already
    # exists is refused as "not implemented" rather than as a bad request.
    managed.ResumeNotSupported: 501,
}


def _upload_status(exc: managed.UploadRejected) -> int:
    for kind, status in _UPLOAD_STATUS.items():
        if isinstance(exc, kind):
            return status
    return 400


def _auto_view(meta: dict | None) -> dict:
    """The run-explanation keys for the run fragment.

    ``profile`` / ``knobs`` are the **resolved** values the run meta recorded;
    ``explanations`` is the CLI's own wording, reused verbatim, for whichever of
    ``--backend auto`` / ``--auto`` ran (backend first, the CLI's order). The
    meta records that wording as a stable ID+parameters (``message``); the
    console renders it with ``tr`` here, so the explanation is translated even
    though the CLI composed it. A run recorded before ``message`` existed falls
    back to the English ``explanation``. Every key is empty when the meta has
    neither, so a run without auto renders unchanged.
    """
    meta = meta or {}
    explanations = []
    for key in ("backend_auto", "auto"):
        section = meta.get(key)
        if not section:
            continue
        message = section.get("message")
        if message is not None:
            explanations.append(render_service_message(message, tr))
        elif section.get("explanation"):
            explanations.append(section["explanation"])
    return {
        "profile": meta.get("profile"),
        "knobs": meta.get("decoder_knobs") or {},
        "explanations": explanations,
    }


def _run_context(
    state: RunState | None, *, meeting_id: int, fallback=None, meta: dict | None = None
) -> dict:
    """The template context for one run fragment (live state, else the last row)."""
    if state is not None:
        context = state.summary()
        last = state.last
        context["meeting_id"] = state.meeting_id
        context["message"] = last.message if last else ""
        context["polling"] = state.status in ("queued", "running")
        context.update(_auto_view(meta))
        return context
    context = {
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
    context.update(_auto_view(meta if meta is not None else fallback.options))
    return context


#: How many transcript segments the meeting view shows per page. The transcript
#: is paged rather than rendered whole: a multi-hour tape is tens of thousands of
#: ``HH:MM:SS.mmm [speaker] text`` lines, which no reviewer reads in one scroll.
TRANSCRIPT_PAGE = 500


def error_message(exc: BaseException) -> str:
    """One user-facing error, translated when the service supplied a message ID.

    A service error that carries ``msgid``/``params`` (``managed.UploadRejected``,
    ``MeetingAgentError``) is rendered through ``tr`` here, at the presentation
    boundary; anything else is its own English diagnostic, passed through
    unchanged rather than restating a rule the service owns.
    """
    msgid = getattr(exc, "msgid", None)
    if isinstance(msgid, str):
        return tr(msgid, **getattr(exc, "params", {}))
    return str(exc)


def draft_view(draft) -> dict:
    """A draft as the templates render it: the shared view plus display fields.

    Everything factual comes from :func:`~clear_record.service.describe_draft`
    (the same shape the MCP adapter returns), so the browser and an agent cannot
    disagree about a draft; only the short checksum and the label are added here.
    """
    view = describe_draft(draft)
    provenance = view["provenance"]
    digest = provenance.get("context_hash") or ""
    view["context_hash_short"] = digest[:12]
    prompt = provenance.get("prompt_hash") or ""
    view["prompt_hash_short"] = prompt[:12]
    return view


# --- webhooks: delivery health (ADR-0020) ---------------------------------- #
# The label for each overall webhook state. Each branch calls ``tr`` with a
# literal so the catalog tooling can extract it; a state the service adds later
# falls through to its own word rather than vanishing.
def _webhook_state_label(state: str) -> str:
    if state == "not_configured":
        return tr("Not configured")
    if state == "config_problem":
        return tr("Configuration problem")
    if state == "delivery_failed":
        return tr("A delivery is failing")
    return tr("Configured")


def _webhook_health_label(health: str) -> str:
    if health == "config_problem":
        return tr("Config problem")
    if health == "delivery_failed":
        return tr("Failing")
    if health == "delivered":
        return tr("Delivered")
    return tr("No delivery yet")


def webhook_status_view(status: WebhookStatus) -> dict:
    """The console's webhook view: overall state, config problems, per-endpoint last delivery.

    Built field by field rather than by ``dataclasses.asdict`` on purpose: the
    endpoint view names only what a reader needs, so no field that could ever
    carry secret material can leak into a template or the JSON API by accident.
    The signing secret is read from the environment at delivery time and never
    stored (ADR-0020); the config problems are the emitter's own strings —
    ``tr``nslated for display, never re-derived — and even those name the
    *environment variable*, never its value. The endpoint carries only *whether*
    it is signed.

    ``state`` comes from :attr:`WebhookStatus.state`, so "not configured",
    "config broken" and "delivery failing" stay distinct.
    """
    endpoints = []
    for report in status.endpoints:
        last = report.last_delivery
        endpoints.append(
            {
                "name": report.name,
                "url": report.url,
                "events": list(report.events),
                "include_content": report.include_content,
                "signed": report.signed,
                "health": report.health,
                "health_label": _webhook_health_label(report.health),
                "problems": list(report.problems),
                "last_delivery": None
                if last is None
                else {
                    "outcome": "delivered" if last.status == "delivered" else "failed",
                    "outcome_label": tr("Delivered")
                    if last.status == "delivered"
                    else tr("Failed"),
                    "at": last.at,
                    "attempts": last.attempts,
                    "http_status": last.http_status,
                    "error": last.error,
                    "event_type": last.type,
                    "event_id": last.event_id,
                },
            }
        )
    return {
        "state": status.state,
        "state_label": _webhook_state_label(status.state),
        "configured": bool(status.endpoints),
        "problems": list(status.problems),
        "endpoints": endpoints,
    }


def create_app(
    registry: Registry,
    runs: RunManager | None = None,
    *,
    trusted_hosts: Sequence[str] | None = None,
    webhooks: WebhookEmitter | None = None,
    agent_runner: Runner | None = None,
    agent_config: AgentConfig | None = None,
) -> FastAPI:
    """Build the app around an opened registry (inject a temp one in tests).

    ``runs`` is the background run manager; inject one with a fake pipeline in
    tests so a whole run lifecycle is exercised with no ASR backend. It defaults
    to the real manager over the same registry.

    ``trusted_hosts`` overrides the extra hostnames the request guard accepts
    (default: ``CR_TRUSTED_HOSTS``, on top of loopback); tests and embedders can
    pass an explicit set, and ``()`` pins the loopback-only default.

    ``webhooks`` is the emitter whose health the console reports; it defaults to
    the shared config-driven one the runs already deliver through, and a test can
    inject an inert or a failing one to exercise the status surface offline.

    ``agent_runner`` / ``agent_config`` are the agent-task seam's two injection
    points, mirroring ``runs``: a test injects a runner so a launch never touches
    an endpoint, and an embedder can pin a config. With neither, the process's
    resolved config (:func:`clear_record.service.default_config`) is read **once**
    here rather than per request, so a page render never re-reads the config file.
    """
    # The console's user-facing text is translated per process. A CLI ``--lang``
    # (or an explicit ``install``) has already chosen; otherwise honour
    # ``CR_LANG``/``LANG``. With neither, the catalog stays null and the English
    # source renders — the byte-identical default.
    install_if_unset()
    runs = runs or RunManager(registry)
    # The emitter the runs already deliver through (``RunManager`` defaults to
    # the shared one), so the console reports on the same delivery the runs make.
    # Injecting one lets a test drive the status surface with no config file.
    emitter = webhooks if webhooks is not None else default_emitter()
    if agent_runner is None and agent_config is None:
        agent_config = default_config()
    app = FastAPI(
        title="clear-record",
        summary="Local project console: projects, glossary, meetings and runs.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    # The manager is the app's own run queue; exposing it lets the process
    # supervisor stop draining cleanly on shutdown (``serve``), and lets an
    # embedder reach the same seam.
    app.state.runs = runs
    app.state.webhooks = emitter
    app.state.agent_runner = agent_runner
    app.state.agent_config = agent_config
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
        """Every project with its term and meeting counts, newest first.

        The counts are the workspace at-a-glance facts (the project page owns
        the full media inventory). Both are cheap registry reads.
        """
        counts = registry.term_counts()
        return [
            {
                "project": project,
                "term_count": counts.get(project.slug, 0),
                "meeting_count": len(registry.list_meetings(project.slug)),
            }
            for project in registry.list_projects()
        ]

    def meeting_rows(slug: str) -> list[dict]:
        rows: list[dict] = []
        for meeting in registry.list_meetings(slug):
            tape_set = registry.latest_recording_set(meeting.id)
            latest = registry.list_runs(meeting.id)
            state = runs.state(latest[0].id) if latest else None
            if state is not None:
                run = _run_context(state, meeting_id=meeting.id, meta=latest[0].options)
            elif latest:
                run = _run_context(
                    None,
                    meeting_id=meeting.id,
                    fallback=latest[0],
                    meta=latest[0].options,
                )
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
        row = registry.get_run(state.run_id)
        return TEMPLATES.TemplateResponse(
            request,
            "_run.html",
            {
                "run": _run_context(
                    state,
                    meeting_id=state.meeting_id,
                    meta=row.options if row else None,
                )
            },
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
                    **_auto_view(None),
                }
            },
        )

    def project_context(
        slug: str, tab: str = "overview", error: str | None = None
    ) -> dict:
        """Everything one project sub-tab renders, from the service only.

        Each tab loads only what it shows, so a render (or a mutation's
        re-render) never pays for the surfaces the operator is not looking at.
        Raises ``KeyError`` for an unknown slug so each caller owns its 404:
        the fragment route returns an ``HTTPException`` (htmx must not swap a
        4xx), while the page route renders the not-found page.
        """
        project = registry.require_project(slug)
        context: dict = {"project": project, "tab": tab, "error": error}
        if tab == "meetings":
            context.update(
                meetings=meeting_rows(slug),
                backends=BACKEND_CHOICES,
                profiles=PROFILE_CHOICES,
                profile_default=PROFILE_CUSTOM,
                profile_options=profile_preview(PROFILE_CUSTOM),
                archives=archive_rows(slug),
            )
        elif tab == "glossary":
            context.update(terms=registry.list_terms(slug), statuses=TERM_STATUSES)
        elif tab == "media":
            context["media"] = media_rows(slug)
        else:
            meetings = registry.list_meetings(slug)
            context.update(
                meeting_count=len(meetings),
                term_count=len(registry.list_terms(slug)),
                recent=[
                    {"meeting": meeting, "managed": managed.is_managed(meeting)}
                    for meeting in meetings[:RECENT_MEETINGS]
                ],
                minutes=minutes_rows(slug),
            )
        return context

    def media_rows(slug: str) -> list[dict]:
        """Every meeting's tapes and transcript summary, from the service only.

        Tapes are :func:`managed.meeting_storage` — the same accounting the
        storage panel and the upload guard use — and the transcript summary is
        :func:`read_transcript`, the read the MCP adapter and the review share.
        The Media tab is an inventory: it restates no storage or transcript rule.
        """
        rows: list[dict] = []
        for meeting in registry.list_meetings(slug):
            usage = managed.meeting_storage(registry, meeting)
            try:
                transcript = read_transcript(meeting, limit=1)
            except FileNotFoundError:
                transcript = None
            rows.append(
                {
                    "meeting": meeting,
                    "workspace_path": usage["workspace_path"],
                    "managed": usage["managed"],
                    "tapes": [
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
                    ],
                    "transcript": None
                    if transcript is None
                    else {"segments": transcript.total, "source": transcript.source},
                }
            )
        return rows

    def detail(
        request: Request,
        slug: str,
        tab: str = "overview",
        error: str | None = None,
    ) -> HTMLResponse:
        try:
            context = project_context(slug, tab, error)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no project {slug!r}") from exc
        return TEMPLATES.TemplateResponse(request, "_detail.html", context)

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
        if isinstance(exc, managed.InvalidUploadId):
            return tr("Invalid upload id")
        if isinstance(exc, managed.ResumeNotSupported):
            return tr("Resuming is not supported")
        return tr("Upload refused")

    def refusal(exc: managed.UploadRejected, label: str) -> dict:
        """A guard failure as the panel shows it: a translated label + the
        service's message.

        The reason is the service's own message ID, rendered here with ``tr`` at
        the presentation boundary — one rule, one home: the guard that enforces
        the rule supplies the ID and its facts, and the console only translates.
        """
        return {"label": label, "reason": tr(exc.msgid, **exc.params)}

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
        workspace and tape sizes, the managed root, free space on it and the
        tapes. Free space is ``meeting_storage['free_bytes']`` — the **same**
        ``root_free_bytes`` the upload guard checks, so the panel and the guard
        can never disagree about the disk. Formatting it for a human is the only
        thing this view does with it.
        """
        usage = managed.meeting_storage(registry, meeting)
        free_bytes = usage["free_bytes"]
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

    # --- HTML views: one meeting's review surface (ticket 16) --------------- #
    def meeting_agent(meeting) -> MeetingAgent:
        """The agent seam for one meeting, over the app's injected plumbing."""
        return MeetingAgent(
            registry,
            meeting,
            runner=app.state.agent_runner,
            config=app.state.agent_config,
        )

    def agent_ready() -> bool:
        """Whether a launch has a runner at all (a display hint, not a guard)."""
        if app.state.agent_runner is not None:
            return True
        config = app.state.agent_config
        return bool(config and (config.endpoint or config.commands))

    def artifact_text(artifact) -> str | None:
        """An artifact's file contents, or ``None`` when the file is gone."""
        try:
            return Path(artifact.path).read_text(encoding="utf-8")
        except OSError:
            return None

    def meeting_context(meeting, *, offset: int = 0, error: str | None = None) -> dict:
        """Everything the meeting review view renders, from the service only.

        The transcript is the same :func:`read_transcript` the MCP adapter uses
        (paged here), the artifacts are the registry's rows, and the drafts are
        read back from the workspace with
        :meth:`~clear_record.service.MeetingAgent.drafts`, so an accepted result
        and its provenance are shown rather than re-derived.
        """
        try:
            page = read_transcript(meeting, offset=offset, limit=TRANSCRIPT_PAGE)
            transcript = {
                "text": page.text,
                "source": page.source,
                "total": page.total,
                "offset": page.offset,
                "returned": page.returned,
                "next": page.next,
                "prev": max(0, page.offset - TRANSCRIPT_PAGE) if page.offset else None,
            }
            transcript_error = None
        except FileNotFoundError:
            transcript = None
            transcript_error = tr("No transcript yet. Run the pipeline first.")
        agent = meeting_agent(meeting)
        minutes_artifact = agent.minutes_artifact()
        return {
            "project": registry.require_project(meeting.project_slug),
            "meeting": meeting,
            "transcript": transcript,
            "transcript_error": transcript_error,
            "page_size": TRANSCRIPT_PAGE,
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "sha256": artifact.sha256,
                    "sha256_short": (artifact.sha256 or "")[:12],
                    "bytes": artifact.bytes,
                    "produced_by": artifact.produced_by,
                    "review_state": artifact.review_state,
                    "created_at": artifact.created_at,
                    "run_id": artifact.run_id,
                }
                for artifact in registry.list_artifacts(meeting.id)
            ],
            "drafts": [draft_view(draft) for draft in agent.drafts()],
            "agent_ready": agent_ready(),
            "minutes_artifact": minutes_artifact,
            "minutes_text": artifact_text(minutes_artifact)
            if minutes_artifact is not None
            else None,
            "error": error,
        }

    def render_meeting(
        request: Request, meeting, *, offset: int = 0, error: str | None = None
    ) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "_meeting.html",
            meeting_context(meeting, offset=offset, error=error),
        )

    def minutes_rows(slug: str) -> list[dict]:
        """Each meeting of a project that has accepted minutes, newest first.

        "Minutes across the project" is exactly the latest ``minutes`` artifact
        per meeting (:meth:`Registry.latest_artifact`), so a re-accepted draft
        supersedes the earlier one instead of appearing twice.
        """
        rows: list[dict] = []
        for meeting in registry.list_meetings(slug):
            artifact = registry.latest_artifact(meeting.id, "minutes")
            if artifact is None:
                continue
            text = artifact_text(artifact) or ""
            heading = next(
                (line.strip() for line in text.splitlines() if line.strip()), ""
            )
            rows.append(
                {"meeting": meeting, "artifact": artifact, "heading": heading[:120]}
            )
        rows.sort(key=lambda row: row["artifact"].id, reverse=True)
        return rows

    # --- full pages (real URLs; hx-boost for speed, plain links without JS) --- #
    def page(
        request: Request,
        template: str,
        *,
        nav: str,
        status_code: int = 200,
        **extra,
    ) -> HTMLResponse:
        """A full page extending ``base.html``.

        The header needs two facts on every page: which top-level nav item is
        current, and whether setup is still incomplete (which shows the Setup
        link, ADR-0027). Injecting them here means a page render cannot forget
        either.
        """
        return TEMPLATES.TemplateResponse(
            request,
            template,
            {"nav": nav, "setup_incomplete": not agent_ready(), **extra},
            status_code=status_code,
        )

    def recent_meetings(projects: list[dict]) -> list[dict]:
        """The newest meetings across every project, for the landing's one line.

        A cheap registry read only: ``list_meetings()`` is newest first and
        carries each meeting's project slug, whose name comes from the already
        loaded project rows. Nothing here walks a workspace — the full tape and
        transcript inventory is the project's Media tab (ADR-0027).
        """
        names = {row["project"].slug: row["project"].name for row in projects}
        return [
            {
                "meeting": meeting,
                "project_name": names.get(meeting.project_slug, meeting.project_slug),
            }
            for meeting in registry.list_meetings()[:RECENT_MEETINGS]
        ]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        """The Projects workspace: the console lands here, not on a dashboard."""
        projects = project_rows()
        return page(
            request,
            "index.html",
            nav="projects",
            projects=projects,
            recent=recent_meetings(projects),
        )

    def project_page(request: Request, slug: str, tab: str) -> HTMLResponse:
        """A project page for one sub-tab, or the not-found page.

        The tab is already validated by which route called this; the unknown
        project (``KeyError``) is what each page route owns. The sidebar list
        and the active-project mark ride along on every tab.
        """
        try:
            context = project_context(slug, tab)
        except KeyError:
            return page(
                request,
                "404.html",
                nav="projects",
                status_code=404,
                message=tr("No project named {slug}.", slug=slug),
            )
        return page(
            request,
            "project.html",
            nav="projects",
            active_slug=slug,
            projects=project_rows(),
            **context,
        )

    @app.get("/projects/{slug}", response_class=HTMLResponse)
    def page_project(request: Request, slug: str) -> HTMLResponse:
        """A project page: the URL is the source of truth for the selection.

        The default tab is Overview; every other tab is its own URL, so a
        refresh and the back button keep the operator where they were.
        """
        return project_page(request, slug, "overview")

    @app.get("/projects/{slug}/meetings", response_class=HTMLResponse)
    def page_project_meetings(request: Request, slug: str) -> HTMLResponse:
        """The Meetings tab: the operational surface."""
        return project_page(request, slug, "meetings")

    @app.get("/projects/{slug}/glossary", response_class=HTMLResponse)
    def page_project_glossary(request: Request, slug: str) -> HTMLResponse:
        """The Glossary tab: the project's terms."""
        return project_page(request, slug, "glossary")

    @app.get("/projects/{slug}/media", response_class=HTMLResponse)
    def page_project_media(request: Request, slug: str) -> HTMLResponse:
        """The Media tab: every meeting's tapes and transcripts."""
        return project_page(request, slug, "media")

    @app.get("/projects/{slug}/meetings/{meeting_slug}", response_class=HTMLResponse)
    def page_meeting(
        request: Request, slug: str, meeting_slug: str, offset: int = 0
    ) -> HTMLResponse:
        """One meeting's review as its own page (transcript, artifacts, drafts).

        The URL is the source of truth, so a refresh or a shared link keeps the
        review; the review's own controls still swap the ``#detail`` fragment.
        """
        meeting = registry.get_meeting(slug, meeting_slug)
        if meeting is None:
            return page(
                request,
                "404.html",
                nav="projects",
                status_code=404,
                message=tr("No meeting named {meeting}.", meeting=meeting_slug),
            )
        return page(
            request,
            "meeting.html",
            nav="projects",
            active_slug=slug,
            projects=project_rows(),
            tab="meetings",
            **meeting_context(meeting, offset=max(0, offset)),
        )

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request) -> HTMLResponse:
        """Settings lands on the first section, not an empty overview."""
        return page(
            request,
            "settings.html",
            nav="settings",
            section="agent",
            settings_sections=SETTINGS_SECTIONS,
        )

    @app.get("/settings/{section}", response_class=HTMLResponse)
    def page_settings_section(request: Request, section: str) -> HTMLResponse:
        """One Settings section, or the not-found page for an unknown slug."""
        if section not in tuple(one for one, _ in SETTINGS_SECTIONS):
            return page(
                request,
                "404.html",
                nav="settings",
                status_code=404,
                message=tr("No settings section named {name}.", name=section),
            )
        return page(
            request,
            "settings.html",
            nav="settings",
            section=section,
            settings_sections=SETTINGS_SECTIONS,
        )

    @app.get("/setup", response_class=HTMLResponse)
    @app.get("/setup/agent", response_class=HTMLResponse)
    def page_setup(request: Request) -> HTMLResponse:
        """Setup: system readiness. The agent flow is one reusable panel with
        two entry points (here and Settings -> Agent); this ticket wires them."""
        return page(request, "setup.html", nav="setup")

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
            request, "_projects.html", {"projects": project_rows(), "active_slug": None}
        )

    @app.post("/ui/projects", response_class=HTMLResponse)
    def ui_create_project(request: Request, name: str = Form(...)) -> HTMLResponse:
        try:
            registry.create_project(name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TEMPLATES.TemplateResponse(
            request, "_projects.html", {"projects": project_rows(), "active_slug": None}
        )

    @app.get("/ui/projects/{slug}", response_class=HTMLResponse)
    def ui_project(request: Request, slug: str) -> HTMLResponse:
        """The Overview tab as a fragment (the project page's default)."""
        return detail(request, slug, tab="overview")

    @app.get("/ui/projects/{slug}/meetings", response_class=HTMLResponse)
    def ui_project_meetings(request: Request, slug: str) -> HTMLResponse:
        return detail(request, slug, tab="meetings")

    @app.get("/ui/projects/{slug}/glossary", response_class=HTMLResponse)
    def ui_project_glossary(request: Request, slug: str) -> HTMLResponse:
        return detail(request, slug, tab="glossary")

    @app.get("/ui/projects/{slug}/media", response_class=HTMLResponse)
    def ui_project_media(request: Request, slug: str) -> HTMLResponse:
        return detail(request, slug, tab="media")

    # --- HTML views: a meeting's transcript, artifacts and agent tasks ------ #
    @app.get("/ui/projects/{slug}/meetings/{meeting_slug}", response_class=HTMLResponse)
    def ui_meeting(
        request: Request, slug: str, meeting_slug: str, offset: int = 0
    ) -> HTMLResponse:
        """One meeting's review surface, as an htmx fragment.

        A fragment rather than a full page, exactly like the project detail: the
        console's navigation is htmx swaps into ``#detail``, so a project and a
        meeting are views of the same single-page shell.
        """
        meeting = registry.get_meeting(slug, meeting_slug)
        if meeting is None:
            raise HTTPException(
                status_code=404, detail=f"no meeting {meeting_slug!r} in {slug!r}"
            )
        return render_meeting(request, meeting, offset=max(0, offset))

    @app.post("/ui/meetings/{meeting_id}/agent/{kind}", response_class=HTMLResponse)
    def ui_run_agent_task(request: Request, meeting_id: int, kind: str) -> HTMLResponse:
        """Launch one agent task for a meeting and re-render the meeting view.

        A refused launch (no configured agent, no transcript, a runner failure)
        re-renders the view with the service's own message as a 200, so htmx
        swaps it in and the operator can fix the cause in place.
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        if kind not in TASK_KINDS:
            raise HTTPException(status_code=404, detail=f"no agent task {kind!r}")
        try:
            meeting_agent(meeting).launch(kind)
        except AgentTaskError as exc:
            return render_meeting(request, meeting, error=error_message(exc))
        return render_meeting(request, meeting)

    def review_draft(request: Request, meeting_id: int, run_id: str, accept: bool):
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        agent = meeting_agent(meeting)
        draft = agent.draft(run_id)
        if draft is None:
            return render_meeting(
                request,
                meeting,
                error=tr("No draft {run_id} for this meeting.", run_id=run_id),
            )
        try:
            if accept:
                agent.promote(draft)
            else:
                agent.reject(draft)
        except AgentTaskError as exc:
            return render_meeting(request, meeting, error=error_message(exc))
        return render_meeting(request, meeting)

    @app.post(
        "/ui/meetings/{meeting_id}/agent/drafts/{run_id}/accept",
        response_class=HTMLResponse,
    )
    def ui_accept_draft(request: Request, meeting_id: int, run_id: str) -> HTMLResponse:
        """Accept a draft: promote it into what its kind produces, then show it."""
        return review_draft(request, meeting_id, run_id, accept=True)

    @app.post(
        "/ui/meetings/{meeting_id}/agent/drafts/{run_id}/reject",
        response_class=HTMLResponse,
    )
    def ui_reject_draft(request: Request, meeting_id: int, run_id: str) -> HTMLResponse:
        """Reject a draft, keeping it and its provenance as history."""
        return review_draft(request, meeting_id, run_id, accept=False)

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
        return detail(request, slug, tab="glossary")

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
        return detail(request, term.project_slug, tab="glossary")

    @app.delete("/ui/glossary/{term_id}", response_class=HTMLResponse)
    def ui_delete_term(request: Request, term_id: int) -> HTMLResponse:
        term = registry.get_term(term_id)
        if term is None:
            raise HTTPException(status_code=404, detail=f"no term {term_id}")
        registry.delete_term(term_id)
        return detail(request, term.project_slug, tab="glossary")

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
            # re-render the tab with the service's message.
            return detail(request, slug, tab="meetings", error=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return detail(request, slug, tab="meetings")

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
        return detail(request, meeting.project_slug, tab="meetings")

    @app.get("/ui/meetings/{meeting_id}/storage", response_class=HTMLResponse)
    def ui_meeting_storage(request: Request, meeting_id: int) -> HTMLResponse:
        """One meeting's storage panel: resolved path, sizes, tapes, controls."""
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return render_storage(request, meeting)

    @app.post("/ui/meetings/{meeting_id}/tapes/upload", response_class=HTMLResponse)
    async def ui_upload_tape(
        request: Request, meeting_id: int, upload_id: str | None = None
    ) -> HTMLResponse:
        """Receive one tape and re-render the panel (ADR-0024).

        The service's guards are the same ones the JSON endpoint uses. A refusal
        re-renders the panel with the guard's translated label and its own
        message as a 200, so htmx swaps the error into place and the form stays
        usable. An optional ``upload_id`` query parameter names the transfer; the
        console sends none, and this node still cannot resume one.
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        declared = _content_length(request)
        try:
            meeting = managed.precheck_upload(
                registry, meeting, declared, upload_id=upload_id
            )
            try:
                form = await request.form()
            except Exception as exc:  # noqa: BLE001 - a malformed body is a refusal
                raise managed.UploadRejected(
                    deferred("could not read the multipart upload: {error}"), error=exc
                ) from exc
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or not upload.filename:
                raise managed.UploadRejected(
                    deferred("attach the tape as a multipart file part named 'file'")
                )
            await run_in_threadpool(
                managed.upload_tape,
                registry,
                meeting,
                upload.file,
                filename=upload.filename,
                declared_bytes=declared,
                upload_id=upload_id,
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
            # tab with the message rather than an error status htmx skips.
            return detail(request, meeting.project_slug, tab="meetings", error=str(exc))
        return detail(request, meeting.project_slug, tab="meetings")

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
        auto: bool = Form(False),
    ) -> HTMLResponse:
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        try:
            # The picker's ``custom`` is the console's "no preset" state (it has
            # no separate unset), so it is passed as an unset profile — exactly
            # what lets the opt-in ``--auto`` choose one.
            resolved = resolve_run(
                PipelineOptions(
                    backend=backend, model=model or None, language=language or None
                ),
                profile=None if profile == PROFILE_CUSTOM else profile,
                auto=auto,
                directory=meeting.workspace_path,
            )
        except ModelNotOnDisk as exc:
            # The service's own message ID, rendered at this boundary — the rule
            # lives in the exception, not restated here.
            return render_run_error(request, meeting_id, exc.message.render(tr))
        except NoBackendAvailable as exc:
            return render_run_error(request, meeting_id, exc.message.render(tr))
        except ValueError as exc:
            return render_run_error(request, meeting_id, tr(str(exc)))
        try:
            run = runs.start(meeting, resolved.options, auto=resolved.meta)
        except ValueError as exc:
            # A conflict while a run is live: re-render the live fragment so its
            # polling is not torn down by an error response (htmx skips 4xx swaps).
            active = runs.active_state(meeting_id)
            if active is not None:
                return render_run(request, active)
            return render_run_error(request, meeting_id, tr(str(exc)))
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

    @app.get("/ui/webhooks", response_class=HTMLResponse)
    def ui_webhooks(request: Request) -> HTMLResponse:
        """The webhook status panel: config validity and the last delivery (ADR-0020).

        Reads the emitter the runs already deliver through — its existing
        ``problems`` surface and its bounded delivery history — so a config
        mistake is visible on the console, not only on stderr, and silence
        ("not configured") stays distinct from success and failure.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "_webhooks.html",
            {"status": webhook_status_view(emitter.status())},
        )

    # --- guided agent setup (ADR-0018's onboarding half, ticket 20) -------- #
    def agent_setup_panel(
        request: Request,
        *,
        detection: Detection | None = None,
        error: str | None = None,
        notice: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """The setup panel: the service's state, plus whatever a step just did.

        Every fact is the service's own — the resolved endpoint, the config
        problems, what a probe found — so the console cannot show a different
        endpoint from the one a task would call. This boundary only renders and
        translates (the same split ``refusal()`` uses for an upload guard).
        """
        return TEMPLATES.TemplateResponse(
            request,
            "_agent_setup.html",
            {
                "setup": setup_view(detection=detection),
                "default_model": DEFAULT_SMALL_MODEL,
                # The default-named harness, looked up on PATH. It is a *hint* for
                # the "point at an existing pi-agent" rung, never a fallback: the
                # harness and its client config are both the user's choice.
                "harnesses": find_harness(),
                "pi_agent": PI_AGENT,
                "mcp_entry": mcp_server_entry(),
                "error": error,
                "notice": notice,
            },
            status_code=status_code,
        )

    @app.get("/ui/agent-setup", response_class=HTMLResponse)
    def ui_agent_setup(request: Request) -> HTMLResponse:
        """The setup state, with no probe on a plain render (so a page is instant)."""
        return agent_setup_panel(request)

    @app.get("/ui/agent-setup/detect", response_class=HTMLResponse)
    def ui_agent_setup_detect(request: Request) -> HTMLResponse:
        """Probe the known local servers and test-call the first usable one."""
        return agent_setup_panel(request, detection=detect())

    @app.post("/ui/agent-setup/use", response_class=HTMLResponse)
    def ui_agent_setup_use(
        request: Request,
        endpoint: str = Form(...),
        model: str = Form(""),
        api_key_env: str = Form(""),
    ) -> HTMLResponse:
        """Verify an endpoint with a real call, then record it in the config.

        Verification comes first and is a precondition: an endpoint that cannot
        answer a test call is never written as if it worked — the failure is
        shown with what the endpoint actually said. ``api_key_env`` stays a
        **name**; no value is read here or stored anywhere.
        """
        try:
            verification = verify_endpoint(
                endpoint, model=model or None, api_key_env=api_key_env or None
            )
            if not verification.ok:
                shown = (
                    verification.detail.render(tr)
                    if verification.detail is not None
                    else tr("The endpoint did not answer a test call.")
                )
                return agent_setup_panel(request, error=shown, status_code=400)
            path = write_agent_settings(
                endpoint, model=model or None, api_key_env=api_key_env or None
            )
        except SetupError as exc:
            return agent_setup_panel(
                request, error=exc.message.render(tr), status_code=400
            )
        return agent_setup_panel(
            request,
            notice=tr(
                "Recorded {endpoint} in {path}.",
                endpoint=endpoint,
                path=str(path),
            ),
        )

    @app.post("/ui/agent-setup/pull", response_class=HTMLResponse)
    def ui_agent_setup_pull(
        request: Request,
        endpoint: str = Form(...),
        model: str = Form(""),
    ) -> HTMLResponse:
        """Pull a small model where the server supports it, then record the choice.

        A pull is a download the user asked for here, never a silent one, and the
        model it actually chose is what gets recorded — verified by a test call
        first, so a pull that succeeded but cannot serve does not get written.
        """
        chosen = model.strip() or DEFAULT_SMALL_MODEL
        pulled = pull_model(chosen, endpoint=endpoint)
        if not pulled.ok:
            shown = (
                pulled.detail.render(tr)
                if pulled.detail is not None
                else tr("The model could not be pulled.")
            )
            return agent_setup_panel(request, error=shown, status_code=400)
        verification = verify_endpoint(endpoint, model=pulled.model)
        if not verification.ok:
            shown = (
                verification.detail.render(tr)
                if verification.detail is not None
                else tr("The endpoint did not answer a test call.")
            )
            return agent_setup_panel(request, error=shown, status_code=400)
        try:
            write_agent_settings(endpoint, model=pulled.model)
        except SetupError as exc:
            return agent_setup_panel(
                request, error=exc.message.render(tr), status_code=400
            )
        return agent_setup_panel(
            request,
            notice=tr("Pulled {model} and recorded it.", model=pulled.model),
        )

    @app.post("/ui/agent-setup/mcp/harness", response_class=HTMLResponse)
    def ui_agent_setup_mcp_harness(
        request: Request,
        harness: str = Form(...),
    ) -> HTMLResponse:
        """Point at an existing MCP-capable harness, and remember it.

        The path is always the user's — typed here, or the one ``find_harness``
        found on ``PATH`` and the panel offered. The console never invents a
        location, and the service's ``resolve_harness`` refuses anything it could
        not actually run, so a typo is reported rather than recorded.
        """
        try:
            pointed = resolve_harness(harness)
        except SetupError as exc:
            return agent_setup_panel(
                request, error=exc.message.render(tr), status_code=400
            )
        remember_harness(pointed)
        return agent_setup_panel(
            request,
            notice=tr(
                "Pointed at the agent harness {path}.",
                path=pointed.path or pointed.name,
            ),
        )

    @app.post("/ui/agent-setup/mcp/config", response_class=HTMLResponse)
    def ui_agent_setup_mcp_config(
        request: Request,
        config: str = Form(...),
    ) -> HTMLResponse:
        """Register clear-record's MCP server in the client config the user names.

        **The path is required and is never defaulted.** An external client's
        config location is that client's business and is defined nowhere in this
        repo, so the console asks for it rather than guessing one; the form
        pre-fills only the path a previous run recorded. What is written is the
        one ``mcpServers`` entry :func:`mcp_server_entry` builds — a command and
        its args, with no environment block, because the MCP server needs no
        credential (the agent brings its own model).
        """
        try:
            path = write_mcp_config(config)
        except SetupError as exc:
            return agent_setup_panel(
                request, error=exc.message.render(tr), status_code=400
            )
        return agent_setup_panel(
            request,
            notice=tr(
                "Registered the clear-record MCP server in {path}.",
                path=str(path),
            ),
        )

    @app.get("/api/agent/setup")
    def agent_setup_status(detect_now: bool = False) -> dict:
        """The machine surface for the setup state; ``?detect_now=1`` probes first.

        JSON, so it stays English (the i18n boundary). The key is never part of
        it: ``api_key_env`` is a variable name, and a value is never read.
        """
        found = detect() if detect_now else None
        return setup_view(detection=found).as_dict()

    # --- JSON API (machines, scripts, later MCP) ---------------------------- #
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "registry": str(registry.db_path)}

    @app.get("/api/webhooks")
    def webhooks_status() -> dict:
        """Webhook endpoint health as JSON; see :func:`webhook_status_view`.

        Not configured, config-broken and delivery-failing are distinct
        ``state`` values. The signing secret is never part of this response —
        only whether an endpoint is signed — and the problem text names the
        environment variable, never its value.
        """
        return webhook_status_view(emitter.status())

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

    # --- JSON API: a meeting's agent tasks --------------------------------- #
    def require_meeting_row(meeting_id: int):
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        return meeting

    @app.get("/api/meetings/{meeting_id}/agent")
    def meeting_agent_tasks(meeting_id: int) -> dict:
        """A meeting's agent-task surface: the kinds, the drafts and the minutes."""
        meeting = require_meeting_row(meeting_id)
        agent = meeting_agent(meeting)
        minutes = agent.minutes_artifact()
        return {
            "tasks": list(TASK_KINDS),
            "configured": agent_ready(),
            "drafts": [describe_draft(draft) for draft in agent.drafts()],
            "minutes": None if minutes is None else _out(minutes),
        }

    @app.post("/api/meetings/{meeting_id}/agent/{kind}", status_code=201)
    def run_agent_task(meeting_id: int, kind: str) -> dict:
        """Launch one agent task; the result is a draft, never auto-accepted."""
        meeting = require_meeting_row(meeting_id)
        if kind not in TASK_KINDS:
            raise HTTPException(status_code=404, detail=f"no agent task {kind!r}")
        try:
            draft = meeting_agent(meeting).launch(kind)
        except AgentTaskError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return describe_draft(draft)

    def review_api(meeting_id: int, run_id: str, accept: bool) -> dict:
        meeting = require_meeting_row(meeting_id)
        agent = meeting_agent(meeting)
        draft = agent.draft(run_id)
        if draft is None:
            raise HTTPException(
                status_code=404,
                detail=f"no draft {run_id!r} for meeting {meeting_id}",
            )
        try:
            reviewed = agent.promote(draft) if accept else agent.reject(draft)
        except AgentTaskError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return describe_draft(reviewed)

    @app.post("/api/meetings/{meeting_id}/agent/drafts/{run_id}/accept")
    def accept_agent_draft(meeting_id: int, run_id: str) -> dict:
        """Accept a draft and return what its acceptance produced."""
        return review_api(meeting_id, run_id, accept=True)

    @app.post("/api/meetings/{meeting_id}/agent/drafts/{run_id}/reject")
    def reject_agent_draft(meeting_id: int, run_id: str) -> dict:
        """Reject a draft, keeping it and its provenance on disk."""
        return review_api(meeting_id, run_id, accept=False)

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
    async def upload_tape(
        request: Request, meeting_id: int, upload_id: str | None = None
    ) -> dict:
        """Receive one tape into the meeting's **managed** workspace (ADR-0024).

        Multipart, one file part named ``file``. The guards run first — and the
        size/disk guards run against ``Content-Length`` **before** the body is
        read, so an over-cap or no-space upload is refused without the transfer.
        The bytes then stream to a ``.part`` file and are renamed into place;
        only a complete, checksummed tape is recorded.

        An optional ``upload_id`` names the transfer: it is validated, and it
        becomes the upload's identity on disk so a resumable layer can be added
        later without changing this request. **Resume itself is not built** — a
        dropped multi-GB upload still restarts from zero, and an id whose scratch
        file already exists is refused as unsupported (501).
        """
        meeting = registry.meeting_by_id(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail=f"no meeting {meeting_id}")
        declared = _content_length(request)
        try:
            meeting = managed.precheck_upload(
                registry, meeting, declared, upload_id=upload_id
            )
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
                upload_id=upload_id,
            )
        except managed.UploadRejected as exc:
            raise HTTPException(
                status_code=_upload_status(exc), detail=str(exc)
            ) from exc
        return _out(tape)

    @app.get("/api/meetings/{meeting_id}/storage")
    def meeting_storage(meeting_id: int) -> dict:
        """A managed meeting's workspace size, tapes and root free space (ADR-0024).

        ``free_bytes`` is ``None`` unless the meeting is managed; it comes from
        the same accounting the upload guard checks.
        """
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
            resolved = resolve_run(
                options,
                profile=None if body.profile == PROFILE_CUSTOM else body.profile,
                auto=body.auto,
                directory=meeting.workspace_path,
            )
        except ModelNotOnDisk as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NoBackendAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            run = runs.start(meeting, resolved.options, auto=resolved.meta)
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
    log_config: dict | None = None,
) -> int:
    """Run the console; called by the ``clear-record web`` and ``serve`` handlers.

    ``trusted_hosts`` is forwarded to :func:`create_app` so ``web --tailscale``
    can trust the resolved tailnet name in-process, with no environment variable
    handed to a child (the design decision in ticket 01). ``None`` keeps the
    ``CR_TRUSTED_HOSTS`` default.

    ``log_config`` is forwarded to uvicorn: ``serve`` passes the diagnostics-sink
    config so a headless node's logs land beside every other clear-record record;
    ``None`` keeps uvicorn's own (stderr) logging, which is right for the
    interactive console.
    """
    import uvicorn

    app = create_app(Registry.open(data_dir=data_dir), trusted_hosts=trusted_hosts)
    extra = {} if log_config is None else {"log_config": log_config}
    config = uvicorn.Config(app, host=host, port=port, log_level="info", **extra)
    server = uvicorn.Server(config)
    # Exposed so `POST /api/shutdown` can ask the server to stop — the desktop
    # build has no terminal to interrupt.
    app.state.server = server
    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.run()
    finally:
        # A clean SIGTERM/stop stops the queue draining; a run still executing
        # is left for startup reconciliation on the next boot (one run per node).
        app.state.runs.shutdown()
    return 0


__all__ = ["create_app", "serve"]
