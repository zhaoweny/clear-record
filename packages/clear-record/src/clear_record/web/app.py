"""The FastAPI app for the clear-record web console.

Three pieces, in this order:

- :mod:`clear_record.web.views` builds every **context** a page or a fragment
  renders, as a named function taking the registry, the run manager and the
  request's locale;
- :mod:`clear_record.web.lookup` owns the one rule *"find this project, meeting,
  tape, run, term, archive, draft or settings section — or answer
  that it is not here"* both surfaces ask;
- **this module is the wiring**: the middleware, the route table, the declared
  request and response shapes, and the injected adapters (the registry, the run
  manager, the webhook emitter). A route body finds the thing,
  builds a context and renders it — it does not build one.

Two surfaces over the same **thin** service adapter:

- ``/api/*`` returns JSON — the machine surface the GUI, scripts and
  integrations share. Every route is a small translation of a service call.
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

from clear_record.core import PROFILE_CUSTOM
from clear_record.core import i18n
from clear_record.core.i18n import deferred, install_if_unset, tr, trn
from clear_record.service import (
    BUNDLE_FILENAME,
    RUN_IN_FLIGHT,
    AgentDraftsOut,
    ArchiveOut,
    ArtifactOut,
    DraftView,
    EventOut,
    MalformedRunOptions,
    Meeting,
    MeetingAgent,
    MeetingAgentError,
    MeetingOut,
    ModelNotOnDisk,
    NoBackendAvailable,
    PipelineOptions,
    ProjectCountOut,
    ProjectOut,
    PromotionError,
    Registry,
    RunManager,
    RunOut,
    RunState,
    RunSummary,
    Shape,
    TASK_KINDS,
    Tape,
    TapeOut,
    TapeSetOut,
    TermOut,
    archive_meeting,
    collect_bundle,
    describe_draft,
    format_mib,
    format_rate,
    format_ratio,
    format_seconds,
    managed,
    resolve_run,
    run_hello_check,
    verify_archive,
)
from clear_record.service.agent_flow import (
    download_transcription_model,
    transcription_status,
)
from clear_record.service.archive import ArchiveVerification
from clear_record.service.auto import (
    DEFAULT_MODEL,
    MODEL_LADDER,
    render_message as render_service_message,
)
from clear_record.service.setup import (
    SetupError,
    SetupStatusOut,
    clear_seen_version,
    record_seen_version,
    remember_harness,
    resolve_harness,
    seen_version,
    setup_view,
    write_mcp_config,
)
from clear_record.service.webhooks import WebhookEmitter, default_emitter
from clear_record.web import guard, lookup, views

# --- the process adapters the view seam reads from *this* module ----------- #
# These names are bound here on purpose and are deliberately unused *here*: the
# seam reads them through ``views._adapters()`` (whose docstring lists all six),
# because the console's tests replace them on this module —
# ``available_backend_ids`` (the probe that would otherwise compile and run the
# ASR helper; ``tests/web/conftest.py``, ``tests/web/test_web_api.py``),
# ``models_on_disk`` and ``backend_status`` (``tests/web/test_web_settings.py``)
# and ``find_harness`` (``tests/web/test_web_agent_setup.py``) — while the two the
# routes *also* call, ``verify_archive`` and ``setup_view``, are named with the
# route-called set below. Binding them here is what keeps those patches
# effective: a seam that imported its own copy would go on passing the suite while
# the pins had stopped meaning anything.
#
# Five more service calls are frozen here for the same reason, and their pins are
# on *this* module: the **routes** call them by name, so a route rewritten to
# reach one any other way would leave its pin silently ineffective. They are
# ``transcription_status`` (stubbed for every console test in
# ``tests/web/conftest.py``, overridden in ``tests/web/test_web_setup.py``),
# ``run_hello_check`` (``tests/web/test_web_agent_flow.py``),
# ``download_transcription_model`` (``tests/web/test_web_settings.py``,
# ``tests/web/test_web_setup.py``) and ``serve``
# (``tests/cli/test_serve.py``, ``tests/web/test_web_tailscale.py``). Two names
# belong to *both* groups, and they are the case this paragraph exists for:
# ``verify_archive`` (``tests/web/test_web_api.py``) and ``setup_view``
# (``tests/web/test_web_agent_setup.py``) are read through the seam *and* called
# by a route, so both hazards apply to them at once.
from clear_record.service.auto import (  # noqa: F401
    available_backend_ids,
    models_on_disk,
)
from clear_record.service.diagnostics import backend_status  # noqa: F401
from clear_record.service.setup import find_harness  # noqa: F401

# ``_auto_view``, ``ACTIVE_RUN_LABELS`` and ``SETTINGS_SECTIONS`` moved to the
# view seam; they are imported here because the console's tests read them from
# **this** module (`tests/test_i18n_boundaries.py`, `tests/web/test_web_activity.py`,
# `tests/web/test_web_settings.py`) — the seam moved the code, not the names.
from clear_record.web.views import (  # noqa: F401
    _auto_view,
    ACTIVE_RUN_LABELS,
    SETTINGS_SECTIONS,
)

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
#: The message-node renderer, so a template can render a ``Message`` (an ID plus
#: parameters) composed in a lower layer with the same ``tr`` lookup.
TEMPLATES.env.globals["render_message"] = render_service_message
#: The service's display formatters, so a figure the axes carry (a ratio, a
#: rate, seconds, bytes) renders exactly as the terminal renderer prints it.
TEMPLATES.env.globals["format_ratio"] = format_ratio
TEMPLATES.env.globals["format_rate"] = format_rate
TEMPLATES.env.globals["format_seconds"] = format_seconds
TEMPLATES.env.globals["format_mib"] = format_mib

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


def render_download_error(exc: Exception) -> str:
    """The download step error text: a seam ``Message``, else the raw fault.

    ``download_transcription_model`` raises :class:`SetupError`, whose ``message``
    is a translatable :class:`~clear_record.core.message.Message`; this boundary
    renders it with ``tr`` the same way the other setup routes do. Any other
    failure keeps the ``Type: text`` diagnostic, which is what a user pastes.
    """
    render = getattr(getattr(exc, "message", None), "render", None)
    if render is not None:
        return render(tr)
    return f"{type(exc).__name__}: {exc}"


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


# --- the JSON API's response shapes (ADR-0030) ------------------------------ #
#
# A request body was already a declared model (`ProjectCreate`, `RunCreate`,
# ...); these are the responses. A shape that *is* one of the registry's values
# is derived from that value in `clear_record.service.schemas`; a shape a service
# *function* computes is declared where that function builds it (`DraftView`,
# `MeetingStorage`, `ArchiveVerification`, `SetupStatusOut`, `RunSummary`) —
# except `AgentDraftsOut`, which both edges build out of the draft surface and
# which is declared beside that surface in `service/agent_review.py` — and the
# webhook view, which both surfaces render, is declared beside its builder in
# `clear_record.web.views`. What is left here is this edge's own: a computed
# answer, or an envelope around a value.
#
# Declaring them is what gets an answer *checked* on the way out — for the
# payloads that need the check. FastAPI takes the return annotation as the route's
# `response_model` and validates the returned value against it, which was measured
# by breaking a handler in a copy of the tree: a **dict** payload missing a
# declared field and one carrying a field the model does not declare are both
# refused (`ResponseValidationError`), because `Shape` states `extra="forbid"`.
#
# What that check does *not* cover is a value that is already an instance of the
# declared model: pydantic does not revalidate an instance it is handed
# (`revalidate_instances='never'`, its default), and every handler here returns
# exactly that (`ProjectOut.model_validate(...)`, `Shape.of(...)`). What holds
# those answers to their declaration is the **construction** — `model_validate` at
# the point the shape is built, which is where a missing or mistyped field fails —
# and the edge's check is the net under the payloads an edge assembles as plain
# dicts.
# The caller of a handler whose **dict** payload fails the check gets a bare 500
# (`Internal Server Error`, no detail): loud for a developer, opaque for a client,
# and left as it is on purpose. A handler that returns an instance of its declared
# model is not caught at the edge at all (see above), which is why that shape is
# the one construction has to hold — a handler whose shape does not match its own
# declaration is a bug in this tree either way, not a request a client can fix.
#
# The HTML routes are deliberately not in this list: a template render is not a
# data boundary, and what a template needs is a context, not a shape.


class HealthOut(Shape):
    """`/api/health`: the process is up, and which registry it opened."""

    status: str
    registry: str


class ShutdownOut(Shape):
    """`/api/shutdown`: the managed server has been asked to stop."""

    status: str


class TapeDeletedOut(Shape):
    """`DELETE /api/meetings/{id}/tapes/{tape_id}`: the row that went, and the caveat."""

    deleted: TapeOut
    note: str


class RunSnapshotOut(Shape):
    """One run as the API reports it: the registry row, plus its live state."""

    run: RunOut
    state: RunSummary


class RunEventsOut(Shape):
    """A page of one run's event stream, with the cursor to continue from."""

    events: list[EventOut]
    next: int


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


async def _receive_tape(
    registry: Registry,
    meeting: Meeting,
    request: Request,
    *,
    declared: int | None,
    upload_id: str | None,
) -> Tape:
    """Read one multipart tape body and store it, for both upload surfaces.

    The form parse, the ``file`` part check and the threadpool
    :func:`managed.upload_tape` call are identical on the HTML and JSON routes;
    only how a refusal is presented differs. A malformed body or a missing
    ``file`` part raises the same :class:`managed.UploadRejected` the guards do,
    so each caller maps it to its own shape (a re-render, or an HTTP status).
    """
    # Accepted deviation from ADR-0024:67-71 (which decides multipart -> a sibling
    # .part on the managed root, and defers only resumability): Starlette 1.6
    # parses the multipart body first, and each *file part* over 1 MB spools to a
    # SpooledTemporaryFile under the process TMPDIR before managed.upload_tape
    # streams it to the managed .part. A large upload therefore holds its bytes
    # in TMPDIR as well as on the managed root, and TMPDIR (unlike the managed
    # root) is not covered by precheck_upload's free-space guard. The cost is
    # accepted: precheck_upload still caps the declared size against the managed
    # root before the body is read, and the .part writer maps ENOSPC to
    # InsufficientSpace. Removing the TMPDIR copy needs true streaming, which the
    # ADR does not yet decide (it defers resumability only).
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
    return await run_in_threadpool(
        managed.upload_tape,
        registry,
        meeting,
        upload.file,
        filename=upload.filename,
        declared_bytes=declared,
        upload_id=upload_id,
    )


def create_app(
    registry: Registry,
    runs: RunManager | None = None,
    *,
    trusted_hosts: Sequence[str] | None = None,
    webhooks: WebhookEmitter | None = None,
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
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    extra_hosts = (
        guard.normalize_hosts(trusted_hosts)
        if trusted_hosts is not None
        else guard.trusted_extra_hosts()
    )

    def locale(request: Request) -> str:
        """This request's locale, as the middleware below resolved and installed it."""
        return request.state.locale

    @app.middleware("http")
    async def request_locale(request: Request, call_next):
        """Pick this request's locale and install its catalog.

        A cookie or an ``Accept-Language`` header is a preference the *request*
        carries, so its catalog is installed here (cookie > header > the
        environment tail). With neither, the catalog chosen at startup
        (``CR_LANG``/``LANG``, an explicit ``install``, or a test's ``use``) is
        left untouched — which is also what keeps the pseudo-locale boundary
        tests honest. The contexts the seam builds then speak this locale.

        ``gettext``'s catalog is process-wide, not per-request; that is fine for
        a local single-user console (ADR-0013) whose browser sends
        ``Accept-Language`` on every request, and it is the same global seam
        ``install_if_unset`` already used.
        """
        cookie = request.cookies.get(LANG_COOKIE)
        accept_language = request.headers.get("accept-language")
        if cookie is not None or accept_language is not None:
            chosen = resolve_web_locale(cookie, accept_language)
            i18n.install(chosen)
        else:
            chosen = i18n.current_locale() or i18n.SOURCE_LOCALE
        request.state.locale = chosen
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

    # --- the seam's two entry points: a context, and a template ------------ #
    def render(
        request: Request, template: str, context: dict, *, status_code: int = 200
    ) -> HTMLResponse:
        """Render one template with the context its route built.

        That is a seam-built context — :mod:`clear_record.web.views` — or a
        service value a route passes through (the transcription step, the
        hello-world check).
        """
        return TEMPLATES.TemplateResponse(
            request, template, context, status_code=status_code
        )

    def page(
        request: Request,
        template: str,
        *,
        nav: str,
        status_code: int = 200,
        **extra,
    ) -> HTMLResponse:
        """A full page extending ``base.html``.

        Every page needs the top-level nav item, the header chip's live state
        and the setup marker: the Setup link and the update notice both come
        from the seam's ``setup_flags``, so a page render cannot forget either.
        """
        return render(
            request,
            template,
            views.page_context(registry, locale(request), nav=nav, **extra),
            status_code=status_code,
        )

    def missing_page(request: Request, *, nav: str, message: str) -> HTMLResponse:
        """The console's not-found page: the one rule's answer on a full page.

        A page route catches ``lookup.NotFound`` and renders this, because it
        alone knows the section whose nav the page wears and what to call the
        thing in the reader's words. The machine surfaces — the JSON API and the
        htmx fragments — are answered once by the handler below instead.
        """
        return page(
            request,
            "404.html",
            nav=nav,
            status_code=404,
            message=message,
        )

    def detail(
        request: Request,
        slug: str,
        tab: str = "overview",
        error: str | None = None,
    ) -> HTMLResponse:
        """One project sub-tab as the fragment htmx swaps into ``#detail``."""
        return render(
            request,
            "_detail.html",
            views.project_context(registry, runs, locale(request), slug, tab, error),
        )

    def render_run(request: Request, state: RunState) -> HTMLResponse:
        return render(
            request,
            "_run.html",
            {"run": views.run_context(registry, runs, locale(request), state)},
        )

    def render_run_error(
        request: Request, meeting_id: int, message: str
    ) -> HTMLResponse:
        return render(
            request,
            "_run.html",
            {"run": views.run_refusal_context(meeting_id, message)},
        )

    def render_storage(
        request: Request, meeting: Meeting, error: dict | None = None
    ) -> HTMLResponse:
        return render(
            request,
            "_storage.html",
            {
                "storage": views.storage_context(registry, locale(request), meeting),
                "error": error,
            },
        )

    # --- the app's draft seam (wiring, never a view) ----------------------- #
    def meeting_agent(meeting: Meeting) -> MeetingAgent:
        """The draft seam for one meeting, over the app's registry."""
        return MeetingAgent(registry, meeting)

    def meeting_view(
        request: Request, meeting: Meeting, *, offset: int = 0, error: str | None = None
    ) -> dict:
        """The review view's context, with this app's draft seam wired in."""
        return views.meeting_context(
            registry,
            runs,
            locale(request),
            meeting,
            agent=meeting_agent(meeting),
            offset=offset,
            error=error,
        )

    def render_meeting(
        request: Request, meeting: Meeting, *, offset: int = 0, error: str | None = None
    ) -> HTMLResponse:
        return render(
            request,
            "_meeting.html",
            meeting_view(request, meeting, offset=offset, error=error),
        )

    # --- one answer for a lookup that found nothing ------------------------ #
    @app.exception_handler(lookup.NotFound)
    def lookup_missed(request: Request, exc: lookup.NotFound) -> Response:
        """The machine surfaces' half of the one lookup rule.

        The JSON API, and every fragment route that does not answer its own miss,
        answer ``{"detail": …}`` — the body FastAPI's own ``HTTPException``
        produced for the same miss before the rule had a home — out of the
        sentence :mod:`clear_record.web.lookup` composed, which is never
        translated (``docs/i18n.md``). A **page** route catches ``NotFound``
        itself and re-renders with a message, because a page's miss wears its
        section's nav; the one **fragment** that does is the draft review below
        (``review_draft``), which re-renders the meeting rather than answering a
        404 that htmx would not swap.

        Every miss that can reach this handler carries a sentence; the one kind
        that does not (``lookup.settings_section``) is named only by a page URL,
        so a page route always catches it first.
        """
        return JSONResponse(status_code=404, content={"detail": exc.detail})

    @app.exception_handler(MalformedRunOptions)
    def stored_run_options_refused(
        request: Request, exc: MalformedRunOptions
    ) -> Response:
        """Answer a stored run row this build refuses with what the reader said.

        The refusal is nobody's request: the registry holds a row this build
        cannot read, which used to answer 500 — a traceback for the user and no
        message for a client. It is a 409 (the registry's state conflicts with
        this build), and its text names the run and the fields that failed.

        The two halves of that text go to the two callers it has. The JSON API
        carries ``str(exc)`` as ``detail`` — the English sentence, machine-facing,
        unchanged. A page renders the refusal for a person through
        ``MalformedRunOptions.render``: the sentence's *frame* through ``tr`` and
        the field detail exactly as it stands, because the detail is a field list
        in pydantic's own words and has no catalog entry by design
        (``docs/i18n.md`` names it among the never-translated text).

        A page route gets its message on a page of its own rather than in the
        console's chrome: the header chip reads the same registry, so a chrome
        render would raise the same refusal — which is also why this
        answers every page route, not only the ones that read a run directly.
        """
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        return render(
            request,
            "409.html",
            views.unreadable_row_context(locale(request), exc),
            status_code=409,
        )

    # --- full pages (real URLs; hx-boost for speed, plain links without JS) --- #
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        """The Projects workspace: the console lands here, not on a dashboard.

        A genuine first run — installed, no marker recorded, and no projects yet
        — starts at system readiness instead. Once either exists the workspace
        is never hijacked (ADR-0027); an update shows a notice, not a redirect.
        """
        if seen_version() is None and not registry.list_projects():
            return RedirectResponse("/setup", status_code=303)
        projects = views.project_rows(registry)
        return page(
            request,
            "index.html",
            nav="projects",
            projects=projects,
            recent=views.recent_meetings(registry, projects),
        )

    def project_page(request: Request, slug: str, tab: str) -> HTMLResponse:
        """A project page for one sub-tab, or the not-found page.

        The tab is already validated by which route called this; the unknown
        project is the one lookup rule's miss, which this page answers itself.
        The sidebar list and the active-project mark ride along on every tab.
        """
        try:
            context = views.project_context(registry, runs, locale(request), slug, tab)
        except lookup.NotFound:
            return missing_page(
                request,
                nav="projects",
                message=tr("No project named {slug}.", slug=slug),
            )
        return page(
            request,
            "project.html",
            nav="projects",
            active_slug=slug,
            projects=views.project_rows(registry),
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
        try:
            meeting = lookup.meeting_in(registry, slug, meeting_slug)
        except lookup.NotFound:
            return missing_page(
                request,
                nav="projects",
                message=tr("No meeting named {meeting}.", meeting=meeting_slug),
            )
        return page(
            request,
            "meeting.html",
            nav="projects",
            active_slug=slug,
            projects=views.project_rows(registry),
            tab="meetings",
            **meeting_view(request, meeting, offset=max(0, offset)),
        )

    @app.get("/activity", response_class=HTMLResponse)
    def page_activity(request: Request) -> HTMLResponse:
        """The pipeline status page: what this node is doing right now.

        The queue and the recent outcomes across every project, plus the header
        chip's own answer at full width. A real URL with a plain link, like the
        other pages, so it works with no JavaScript and survives a refresh.
        """
        return page(
            request,
            "activity.html",
            nav="activity",
            **views.activity_context(registry, locale(request)),
        )

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request) -> HTMLResponse:
        """Settings lands on the first section, not an empty overview."""
        return page(
            request,
            "settings.html",
            nav="settings",
            **views.settings_context(
                registry, locale(request), SETTINGS_SECTIONS[0][0]
            ),
        )

    @app.get("/settings/{section}", response_class=HTMLResponse)
    def page_settings_section(request: Request, section: str) -> HTMLResponse:
        """One Settings section, or the not-found page for an unknown slug."""
        try:
            context = views.settings_context(registry, locale(request), section)
        except lookup.NotFound:
            return missing_page(
                request,
                nav="settings",
                message=tr("No settings section named {name}.", name=section),
            )
        return page(request, "settings.html", nav="settings", **context)

    @app.get("/setup", response_class=HTMLResponse)
    def page_setup(request: Request) -> HTMLResponse:
        """Setup: system readiness, a numbered sequence.

        The Transcription step states ASR backend and checkpoint readiness from
        the service (transcription_status), so the wizard says what is actually
        ready instead of describing theory. The Agent step embeds the one agent
        flow (the same #agent-setup mount Settings -> Agent uses), and the Try it
        step points at that flow's Try it check and at the permanent copy in
        Settings -> Status. Nothing here is a second tape implementation.
        """
        return page(
            request,
            "setup.html",
            nav="setup",
            reason=request.query_params.get("reason", ""),
            transcription=transcription_status(),
            model_ladder=MODEL_LADDER,
            default_model=DEFAULT_MODEL,
        )

    @app.post("/ui/setup/download-model", response_class=HTMLResponse)
    async def ui_setup_download_model(request: Request) -> HTMLResponse:
        """Download a transcription checkpoint, on the user's click.

        The one place setup triggers a first-use fetch: explicit, never implicit,
        and the same pinned, checksum-verified download a normal run performs, so
        a manual download and a first transcription install identical bytes. The
        form's ``model`` names a ladder size, or is blank for the backend's own
        default. The refreshed Transcription step is returned so its readiness
        updates in place; a failure is shown in the step rather than raised.
        """
        form = await request.form()
        chosen = str(form.get("model") or "").strip() or None
        error = ""
        try:
            await run_in_threadpool(download_transcription_model, chosen)
        except Exception as exc:  # noqa: BLE001 - a failed download is a state
            error = render_download_error(exc)
        return render(
            request,
            "_setup_transcription.html",
            {
                "transcription": transcription_status(),
                "error": error,
                # A failed download leaves state == "model", so the retry form
                # still renders; without these the select would have no options.
                "model_ladder": MODEL_LADDER,
                "default_model": DEFAULT_MODEL,
            },
        )

    @app.post("/ui/settings/models/download", response_class=HTMLResponse)
    async def ui_settings_download_model(request: Request) -> HTMLResponse:
        """Download a chosen transcription checkpoint from Settings -> Models.

        The picker's action: the same pinned, checksum-verified downloader a
        first run uses, so the size the user chose is what lands on disk. The
        page is returned with the new inventory; a failure is shown, not raised.
        """
        form = await request.form()
        chosen = str(form.get("model") or "").strip() or None
        error = ""
        try:
            await run_in_threadpool(download_transcription_model, chosen)
        except Exception as exc:  # noqa: BLE001 - a failed download is a state
            error = render_download_error(exc)
        return render(
            request,
            "_settings_models.html",
            {"error": error, **views.models_context(locale(request))},
        )

    @app.get("/setup/agent", response_class=HTMLResponse)
    def page_setup_agent(request: Request) -> HTMLResponse:
        """The setup wizard's Agent step as its own URL.

        It mounts the same one flow (#agent-setup -> /ui/agent-setup) that
        /settings/agent mounts, so the two entry points cannot drift.
        """
        return page(request, "agent.html", nav="setup")

    @app.post("/setup/complete")
    def complete_setup() -> RedirectResponse:
        """Leave the wizard having seen this version, and land back in Projects.

        COMPLETE is one of the only two writes that record the marker; visiting
        or skipping a step records nothing, so the wizard cannot vanish
        silently.
        """
        record_seen_version()
        return RedirectResponse("/", status_code=303)

    @app.post("/setup/restart")
    def restart_setup() -> RedirectResponse:
        """Forget the marker: the nav Setup link returns and the wizard re-opens.

        The Settings -> Status walk-setup-again action. It writes no other key,
        so a returning user's harness and MCP client config survive it.
        """
        clear_seen_version()
        return RedirectResponse("/setup", status_code=303)

    @app.post("/setup/dismiss")
    def dismiss_setup() -> RedirectResponse:
        """Dismiss the update notice: the same marker write as COMPLETE."""
        record_seen_version()
        return RedirectResponse("/", status_code=303)

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
        return render(
            request,
            "_projects.html",
            {"projects": views.project_rows(registry), "active_slug": None},
        )

    @app.post("/ui/projects", response_class=HTMLResponse)
    def ui_create_project(request: Request, name: str = Form(...)) -> HTMLResponse:
        """Create a project, or re-render the list with the service's message.

        An invalid name is a typo the user can fix in the form, so it re-renders
        at 200: htmx does not swap a 4xx (base.html sets noSwap), and a 409 here
        would make the failure invisible.
        """
        error = None
        try:
            registry.create_project(name)
        except ValueError as exc:
            error = str(exc)
        response = render(
            request,
            "_projects.html",
            {
                "projects": views.project_rows(registry),
                "active_slug": None,
                "error": error,
            },
        )
        if error is None:
            # The add-project form sits outside the #projects swap target, so the
            # swap does not replace it. Signal a real success so it clears; a
            # refusal carries no signal and keeps the user's text.
            response.headers["HX-Trigger"] = "project-created"
        return response

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

    # --- HTML views: a meeting's transcript, artifacts and drafts ---------- #
    @app.get("/ui/projects/{slug}/meetings/{meeting_slug}", response_class=HTMLResponse)
    def ui_meeting(
        request: Request, slug: str, meeting_slug: str, offset: int = 0
    ) -> HTMLResponse:
        """One meeting's review surface, as an htmx fragment.

        A fragment rather than a full page, exactly like the project detail: the
        console's navigation is htmx swaps into ``#detail``, so a project and a
        meeting are views of the same single-page shell.
        """
        meeting = lookup.meeting_in(registry, slug, meeting_slug)
        return render_meeting(request, meeting, offset=max(0, offset))

    def review_draft(
        request: Request,
        meeting_id: int,
        draft_id: str,
        accept: bool,
        version: int | None = None,
    ):
        meeting = lookup.meeting(registry, meeting_id)
        agent = meeting_agent(meeting)
        try:
            draft = lookup.draft(agent, draft_id)
        except lookup.NotFound:
            return render_meeting(
                request,
                meeting,
                error=tr("No draft {draft_id} for this meeting.", draft_id=draft_id),
            )
        try:
            if accept:
                agent.promote(draft, version=version)
            else:
                agent.reject(draft, version=version)
        except (MeetingAgentError, PromotionError) as exc:
            return render_meeting(
                request, meeting, error=views.error_message(locale(request), exc)
            )
        return render_meeting(request, meeting)

    @app.post(
        "/ui/meetings/{meeting_id}/agent/drafts/{draft_id}/accept",
        response_class=HTMLResponse,
    )
    def ui_accept_draft(
        request: Request,
        meeting_id: int,
        draft_id: str,
        version: int | None = Form(None),
    ) -> HTMLResponse:
        """Accept a draft version: promote it into what its kind produces.

        ``version`` is the version the reviewer read, posted by the form beside
        it; a stale one re-renders with the service's own refusal.
        """
        return review_draft(request, meeting_id, draft_id, accept=True, version=version)

    @app.post(
        "/ui/meetings/{meeting_id}/agent/drafts/{draft_id}/reject",
        response_class=HTMLResponse,
    )
    def ui_reject_draft(
        request: Request,
        meeting_id: int,
        draft_id: str,
        version: int | None = Form(None),
    ) -> HTMLResponse:
        """Reject a draft version, keeping the chain as history."""
        return review_draft(
            request, meeting_id, draft_id, accept=False, version=version
        )

    @app.post("/ui/projects/{slug}/glossary", response_class=HTMLResponse)
    def ui_add_term(
        request: Request,
        slug: str,
        term: str = Form(...),
        reading: str = Form(""),
        aliases: str = Form(""),
        definition: str = Form(""),
    ) -> HTMLResponse:
        lookup.project(registry, slug)
        try:
            registry.add_term(
                slug,
                term,
                reading=reading or None,
                aliases=aliases or None,
                definition=definition or None,
            )
        except ValueError as exc:
            # A bad term is fixable in the form, so re-render the tab with the
            # service's message at 200 (htmx does not swap a 4xx; see base.html).
            return detail(request, slug, tab="glossary", error=str(exc))
        return detail(request, slug, tab="glossary")

    @app.post("/ui/glossary/{term_id}/status", response_class=HTMLResponse)
    def ui_set_status(
        request: Request, term_id: int, status: str = Form(...)
    ) -> HTMLResponse:
        term = lookup.term(registry, term_id)
        try:
            term = registry.update_term(term_id, status=status)
        except ValueError as exc:
            # An invalid status is fixable in the form; re-render the tab with
            # the service's message at 200 (htmx does not swap a 4xx).
            return detail(request, term.project_slug, tab="glossary", error=str(exc))
        return detail(request, term.project_slug, tab="glossary")

    @app.delete("/ui/glossary/{term_id}", response_class=HTMLResponse)
    def ui_delete_term(request: Request, term_id: int) -> HTMLResponse:
        term = lookup.term(registry, term_id)
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
        lookup.project(registry, slug)
        try:
            meeting = registry.create_meeting(
                slug,
                title,
                workspace_path=workspace_path or None,
                recorded_at=recorded_at or None,
            )
            if not workspace_path:
                managed.ensure_managed_workspace(registry, meeting)
        except managed.UploadRejected as exc:
            # The meeting exists but its managed workspace could not be made:
            # re-render the tab with the service's message.
            return detail(request, slug, tab="meetings", error=str(exc))
        except ValueError as exc:
            # A bad title is fixable in the form; re-render the tab with the
            # service's message at 200 (htmx does not swap a 4xx; see base.html).
            return detail(request, slug, tab="meetings", error=str(exc))
        return detail(request, slug, tab="meetings")

    @app.post("/ui/meetings/{meeting_id}/tapes", response_class=HTMLResponse)
    def ui_set_tapes(
        request: Request, meeting_id: int, paths: str = Form("")
    ) -> HTMLResponse:
        meeting = lookup.meeting(registry, meeting_id)
        tapes = [line.strip() for line in paths.splitlines() if line.strip()]
        try:
            registry.set_recording_set(meeting_id, tapes)
        except ValueError as exc:
            # A tape set is fixable in the form, so re-render the tab with the
            # service's message at 200 (htmx does not swap a 4xx; see base.html).
            return detail(request, meeting.project_slug, tab="meetings", error=str(exc))
        return detail(request, meeting.project_slug, tab="meetings")

    @app.get("/ui/meetings/{meeting_id}/storage", response_class=HTMLResponse)
    def ui_meeting_storage(request: Request, meeting_id: int) -> HTMLResponse:
        """One meeting's storage panel: resolved path, sizes, tapes, controls."""
        return render_storage(request, lookup.meeting(registry, meeting_id))

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
        meeting = lookup.meeting(registry, meeting_id)
        declared = _content_length(request)
        try:
            meeting = managed.precheck_upload(
                registry, meeting, declared, upload_id=upload_id
            )
            await _receive_tape(
                registry, meeting, request, declared=declared, upload_id=upload_id
            )
        except managed.UploadRejected as exc:
            return render_storage(
                request,
                meeting,
                error=views.refusal(
                    locale(request), exc, views.upload_error_label(locale(request), exc)
                ),
            )
        return render_storage(request, meeting)

    @app.delete(
        "/ui/meetings/{meeting_id}/tapes/{tape_id}", response_class=HTMLResponse
    )
    def ui_delete_tape(request: Request, meeting_id: int, tape_id: int) -> HTMLResponse:
        """Delete one managed tape, re-rendering the panel."""
        meeting = lookup.meeting(registry, meeting_id)
        lookup.tape(registry, meeting, tape_id)
        try:
            managed.delete_tape(registry, meeting, tape_id)
        except managed.UploadRejected as exc:
            return render_storage(
                request,
                meeting,
                error=views.delete_refusal(locale(request), exc),
            )
        return render_storage(request, meeting)

    @app.delete("/ui/meetings/{meeting_id}/tapes", response_class=HTMLResponse)
    def ui_delete_meeting_tapes(request: Request, meeting_id: int) -> HTMLResponse:
        """Delete every uploaded tape of the meeting (the per-meeting control).

        Still manual and still confirmed: the archive is the durable copy, and
        nothing here deletes on the node's own initiative (owner, 2026-09-15).
        """
        meeting = lookup.meeting(registry, meeting_id)
        try:
            for tape in registry.list_tapes(meeting_id):
                managed.delete_tape(registry, meeting, tape.id)
        except managed.UploadRejected as exc:
            return render_storage(
                request,
                meeting,
                error=views.delete_refusal(locale(request), exc),
            )
        return render_storage(request, meeting)

    @app.post("/ui/meetings/{meeting_id}/archives", response_class=HTMLResponse)
    def ui_archive_meeting(
        request: Request, meeting_id: int, root: str = Form("")
    ) -> HTMLResponse:
        meeting = lookup.meeting(registry, meeting_id)
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
        archive = lookup.archive(registry, archive_id)
        row = {"archive": archive, "verification": views.archive_status(archive)}
        return render(request, "_archive_status.html", {"row": row})

    @app.get("/ui/profile-options", response_class=HTMLResponse)
    def ui_profile_options(
        request: Request, profile: str = PROFILE_CUSTOM
    ) -> HTMLResponse:
        """The resolved-knobs fragment for the profile currently chosen.

        Fetched by the picker's ``hx-get`` on change; the resolver — not the
        template — decides the values, so the preview cannot disagree with the run.
        """
        try:
            preview = views.profile_preview(profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return render(request, "_profile_options.html", {"profile_options": preview})

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
        meeting = lookup.meeting(registry, meeting_id)
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
            run = runs.start(
                meeting, resolved.options, auto=resolved.meta, origin="console"
            )
        except ValueError as exc:
            # A conflict while a run is live: re-render the live fragment so its
            # polling is not torn down by an error response (htmx skips 4xx swaps).
            active = runs.active_state(meeting_id)
            if active is not None:
                return render_run(request, active)
            return render_run_error(request, meeting_id, tr(str(exc)))
        return render_run(request, runs.require_state(run.id))

    @app.post("/ui/runs/{run_id}/cancel", response_class=HTMLResponse)
    def ui_cancel_run(request: Request, run_id: int) -> HTMLResponse:
        """Cancel a queued or running run.

        A queued run is stopped here and now; a running one is *asked* to stop —
        the request is recorded and its owner ends it at a safe boundary (see
        ``RunManager.cancel``). Re-rendering the fragment is the whole response:
        the run's own polling (or the next click) shows the outcome, and a
        cancelled run that is already terminal is a no-op rather than an error.
        """
        lookup.run_state(runs, run_id)
        runs.cancel(run_id)
        return render_run(request, runs.require_state(run_id))

    @app.post("/ui/runs/{run_id}/resume", response_class=HTMLResponse)
    def ui_resume_run(request: Request, run_id: int) -> HTMLResponse:
        """Start a new run continuing ``run_id``, and render it.

        The new run carries the previous run's own resolved options with resume
        forced on, so the chunk cache is what it reuses; the fragment shows the
        link back to the run it continues.
        """
        previous = lookup.run(registry, run_id)
        try:
            run = runs.resume(run_id, origin="console")
        except ValueError as exc:
            # A refusal is the run's own message (already in flight, or no
            # recorded options to continue with), rendered where the user asked.
            return render_run_error(request, previous.meeting_id, tr(str(exc)))
        return render_run(request, runs.require_state(run.id))

    @app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    def ui_run(request: Request, run_id: int) -> HTMLResponse:
        return render_run(request, lookup.run_state(runs, run_id))

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
        return render(
            request,
            "_webhooks.html",
            {"status": views.webhook_status_view(locale(request), emitter.status())},
        )

    # --- agent setup (ADR-0031's onboarding half) -------------------------- #
    def agent_setup_panel(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        part: str = "agent",
    ) -> HTMLResponse:
        """The setup panel: the service's state, plus whatever a step just did.

        Every fact is the service's own — the harness and MCP client config that
        are recorded, whether those paths are still there, and what the 0.2 agent
        configuration this version ignores. The seam builds the context and this
        boundary only renders it. There is no key to ask for: the page would be a
        bug if it prompted for one.
        """
        standalone = part == "mcp"
        return render(
            request,
            "_mcp_setup.html" if standalone else "_agent_setup.html",
            views.agent_setup_context(
                locale(request),
                error=error,
                notice=notice,
                part=part,
            ),
        )

    @app.get("/ui/agent-setup", response_class=HTMLResponse)
    def ui_agent_setup(request: Request, part: str = "agent") -> HTMLResponse:
        """The setup state, as the harness and MCP rungs the page can check."""
        return agent_setup_panel(request, part="mcp" if part == "mcp" else "agent")

    @app.post("/ui/agent-setup/mcp/harness", response_class=HTMLResponse)
    def ui_agent_setup_mcp_harness(
        request: Request,
        harness: str = Form(...),
        part: str = Form("agent"),
    ) -> HTMLResponse:
        """Point at an existing MCP-capable harness, and remember it.

        The path is always the user's — typed here, or the one ``find_harness``
        found on ``PATH`` and the panel offered. The console never invents a
        location, and the service's ``resolve_harness`` refuses anything it could
        not actually run, so a typo is reported rather than recorded.
        """
        panel_part = "mcp" if part == "mcp" else "agent"
        try:
            pointed = resolve_harness(harness)
        except SetupError as exc:
            return agent_setup_panel(
                request, part=panel_part, error=exc.message.render(tr)
            )
        remember_harness(pointed)
        return agent_setup_panel(
            request,
            part=panel_part,
            notice=tr(
                "Pointed at the agent harness {path}.",
                path=pointed.path or pointed.name,
            ),
        )

    @app.post("/ui/agent-setup/mcp/config", response_class=HTMLResponse)
    def ui_agent_setup_mcp_config(
        request: Request,
        config: str = Form(...),
        part: str = Form("agent"),
    ) -> HTMLResponse:
        """Register clear-record's MCP server in the client config the user names.

        **The path is required and is never defaulted.** An external client's
        config location is that client's business and is defined nowhere in this
        repo, so the console asks for it rather than guessing one; the form
        pre-fills only the path a previous run recorded. What is written is the
        one ``mcpServers`` entry :func:`mcp_server_entry` builds — a command and
        its args, with no environment block, because the MCP server needs no
        credential (the harness brings its own model).
        """
        panel_part = "mcp" if part == "mcp" else "agent"
        try:
            path = write_mcp_config(config)
        except SetupError as exc:
            return agent_setup_panel(
                request, part=panel_part, error=exc.message.render(tr)
            )
        return agent_setup_panel(
            request,
            part=panel_part,
            notice=tr(
                "Registered the clear-record MCP server in {path}.",
                path=str(path),
            ),
        )

    @app.post("/ui/hello-check", response_class=HTMLResponse)
    def ui_hello_check(request: Request) -> HTMLResponse:
        """Run the hello-world acceptance check and render its outcome.

        The **same** service call (clear_record.service.agent_flow.run_hello_check)
        the agent flow's Try it stage and Settings -> Status both render: one
        implementation, so a finding shown on Status is the one the flow shows.
        Every anticipated failure is a finding the service returns -- no system
        voice, no backend, no model, or a failed transcription -- so this route
        never turns a diagnostic state into an exception.

        The check creates its own scratch tape/transcript under the state dir and
        runs the real stages; a missing voice or backend returns in milliseconds,
        while a real transcription can take longer (the sync route runs in the
        server's threadpool, so it does not block the event loop).
        """
        result = run_hello_check(lang=locale(request))
        return render(request, "_hello_check.html", {"check": result})

    @app.get("/api/agent/setup")
    def agent_setup_status() -> SetupStatusOut:
        """The machine surface for the agent setup state.

        JSON, so it stays English (the i18n boundary). There is no key and no
        endpoint in it, because this version has neither; ``ignored`` names the
        0.2 configuration the user may still have that nothing reads now.
        """
        return setup_view().as_dict()

    # --- JSON API (machines, scripts and integrations) ---------------------- #
    @app.get("/api/health")
    def health() -> HealthOut:
        return HealthOut(status="ok", registry=str(registry.db_path))

    @app.get("/api/webhooks")
    def webhooks_status(request: Request) -> views.WebhookStatusOut:
        """Webhook endpoint health as JSON; see :func:`views.webhook_status_view`.

        Not configured, config-broken and delivery-failing are distinct
        ``state`` values. The signing secret is never part of this response —
        only whether an endpoint is signed — and the problem text names the
        environment variable, never its value.
        """
        return views.webhook_status_view(locale(request), emitter.status())

    @app.get("/api/projects")
    def list_projects() -> list[ProjectCountOut]:
        counts = registry.term_counts()
        return [
            ProjectCountOut.of(project, term_count=counts.get(project.slug, 0))
            for project in registry.list_projects()
        ]

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> ProjectOut:
        try:
            project = registry.create_project(
                body.name,
                notes=body.notes,
                default_archive_root=body.default_archive_root,
                slug=body.slug,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ProjectOut.model_validate(project)

    @app.get("/api/projects/{slug}")
    def get_project(slug: str) -> ProjectOut:
        return ProjectOut.model_validate(lookup.project(registry, slug))

    @app.patch("/api/projects/{slug}")
    def update_project(slug: str, body: ProjectUpdate) -> ProjectOut:
        lookup.project(registry, slug)
        try:
            project = registry.update_project(
                slug,
                name=body.name,
                notes=body.notes,
                default_archive_root=body.default_archive_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ProjectOut.model_validate(project)

    @app.get("/api/projects/{slug}/glossary")
    def list_terms(slug: str, status: str | None = None) -> list[TermOut]:
        lookup.project(registry, slug)
        try:
            terms = registry.list_terms(slug, status=status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [TermOut.model_validate(term) for term in terms]

    @app.post("/api/projects/{slug}/glossary", status_code=201)
    def add_term(slug: str, body: TermCreate) -> TermOut:
        lookup.project(registry, slug)
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
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TermOut.model_validate(term)

    @app.patch("/api/glossary/{term_id}")
    def update_term(term_id: int, body: TermUpdate) -> TermOut:
        lookup.term(registry, term_id)
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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TermOut.model_validate(term)

    @app.delete("/api/glossary/{term_id}", status_code=204)
    def delete_term(term_id: int) -> None:
        lookup.term(registry, term_id)
        registry.delete_term(term_id)

    # --- JSON API: meetings, tapes and runs --------------------------------- #
    @app.post("/api/projects/{slug}/meetings", status_code=201)
    def create_meeting(slug: str, body: MeetingCreate) -> MeetingOut:
        lookup.project(registry, slug)
        try:
            meeting = registry.create_meeting(
                slug,
                body.title,
                recorded_at=body.recorded_at,
                workspace_path=None if body.managed else body.workspace_path,
            )
            if body.managed:
                meeting = managed.ensure_managed_workspace(registry, meeting)
        except managed.UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return MeetingOut.model_validate(meeting)

    @app.get("/api/projects/{slug}/meetings")
    def list_meetings(slug: str) -> list[MeetingOut]:
        lookup.project(registry, slug)
        return [
            MeetingOut.model_validate(meeting)
            for meeting in registry.list_meetings(slug)
        ]

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting(meeting_id: int) -> MeetingOut:
        return MeetingOut.model_validate(lookup.meeting(registry, meeting_id))

    # --- JSON API: a meeting's drafts -------------------------------------- #
    @app.get("/api/meetings/{meeting_id}/agent")
    def meeting_agent_drafts(meeting_id: int) -> AgentDraftsOut:
        """A meeting's draft surface: the kinds, the chains and the minutes."""
        meeting = lookup.meeting(registry, meeting_id)
        agent = meeting_agent(meeting)
        minutes = agent.minutes_artifact()
        return AgentDraftsOut(
            kinds=list(TASK_KINDS),
            drafts=[describe_draft(draft) for draft in agent.drafts()],
            minutes=None if minutes is None else ArtifactOut.of(minutes),
        )

    def review_api(
        meeting_id: int, draft_id: str, accept: bool, version: int | None = None
    ) -> DraftView:
        meeting = lookup.meeting(registry, meeting_id)
        agent = meeting_agent(meeting)
        draft = lookup.draft(agent, draft_id)
        try:
            reviewed = (
                agent.promote(draft, version=version)
                if accept
                else agent.reject(draft, version=version)
            )
        except (MeetingAgentError, PromotionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return describe_draft(reviewed)

    @app.post("/api/meetings/{meeting_id}/agent/drafts/{draft_id}/accept")
    def accept_agent_draft(
        meeting_id: int, draft_id: str, version: int | None = None
    ) -> DraftView:
        """Accept a draft version and return what its acceptance produced.

        ``?version=N`` names the version the caller read (versions are numbered
        from 1); a stale one is a 400 rather than a decision on unseen text.
        """
        return review_api(meeting_id, draft_id, accept=True, version=version)

    @app.post("/api/meetings/{meeting_id}/agent/drafts/{draft_id}/reject")
    def reject_agent_draft(
        meeting_id: int, draft_id: str, version: int | None = None
    ) -> DraftView:
        """Reject a draft version, keeping the chain on disk as history."""
        return review_api(meeting_id, draft_id, accept=False, version=version)

    @app.put("/api/meetings/{meeting_id}/tapes", status_code=201)
    def set_tapes(meeting_id: int, body: TapesUpdate) -> TapeSetOut:
        lookup.meeting(registry, meeting_id)
        try:
            tape_set = registry.set_recording_set(meeting_id, body.paths)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TapeSetOut.model_validate(tape_set)

    @app.post("/api/meetings/{meeting_id}/tapes", status_code=201)
    async def upload_tape(
        request: Request, meeting_id: int, upload_id: str | None = None
    ) -> TapeOut:
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
        meeting = lookup.meeting(registry, meeting_id)
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
            tape = await _receive_tape(
                registry, meeting, request, declared=declared, upload_id=upload_id
            )
        except managed.UploadRejected as exc:
            raise HTTPException(
                status_code=_upload_status(exc), detail=str(exc)
            ) from exc
        return TapeOut.model_validate(tape)

    @app.get("/api/meetings/{meeting_id}/storage")
    def meeting_storage(meeting_id: int) -> managed.MeetingStorage:
        """A managed meeting's workspace size, tapes and root free space (ADR-0024).

        ``free_bytes`` is ``None`` unless the meeting is managed; it comes from
        the same accounting the upload guard checks.
        """
        meeting = lookup.meeting(registry, meeting_id)
        return managed.meeting_storage(registry, meeting)

    @app.delete("/api/meetings/{meeting_id}/tapes/{tape_id}")
    def delete_tape(meeting_id: int, tape_id: int) -> TapeDeletedOut:
        """Delete one managed tape's file and record.

        The archive is the durable copy; the response says so, and a tape in a
        user-chosen workspace is refused (it is not app-owned data).
        """
        meeting = lookup.meeting(registry, meeting_id)
        lookup.tape(registry, meeting, tape_id)
        try:
            tape = managed.delete_tape(registry, meeting, tape_id)
        except managed.UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TapeDeletedOut(
            deleted=TapeOut.model_validate(tape),
            note=(
                "the archive is the durable copy; archive this meeting before "
                "deleting its tapes if you need to keep it"
            ),
        )

    @app.post("/api/meetings/{meeting_id}/runs", status_code=202)
    def start_run(meeting_id: int, body: RunCreate) -> RunSnapshotOut:
        meeting = lookup.meeting(registry, meeting_id)
        if runs.active_state(meeting_id) is not None:
            # The service's own sentence (see `service.lifecycle.RUN_IN_FLIGHT`),
            # not a copy: the guard and the index must read the same to this
            # client.
            raise HTTPException(status_code=409, detail=RUN_IN_FLIGHT)
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
            run = runs.start(
                meeting, resolved.options, auto=resolved.meta, origin="api"
            )
        except ValueError as exc:
            # A refusal the start path made. The pre-check above reads the live
            # run before anything is written, so a submission that *raced* another
            # client reads nothing there and is refused here instead — the same
            # condition for the same user, and it answers the same 409. The
            # re-read is what tells that apart from the bad requests (no
            # workspace, no tape set), which stay 400.
            if runs.active_state(meeting_id) is not None:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunSnapshotOut(
            run=RunOut.model_validate(run),
            state=runs.require_state(run.id).summary(),
        )

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> RunSnapshotOut:
        run = lookup.run(registry, run_id)
        state = lookup.run_state(runs, run_id)
        return RunSnapshotOut(run=RunOut.model_validate(run), state=state.summary())

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: int, after: int = 0) -> RunEventsOut:
        state = lookup.run_state(runs, run_id)
        events = state.events_since(after)
        return RunEventsOut(
            events=[EventOut.model_validate(event) for event in events],
            next=after + len(events),
        )

    # --- JSON API: archives ------------------------------------------------- #
    @app.post("/api/meetings/{meeting_id}/archives", status_code=201)
    def post_archive(meeting_id: int, body: ArchiveCreate | None = None) -> ArchiveOut:
        meeting = lookup.meeting(registry, meeting_id)
        root = body.root if body else None
        try:
            archive = archive_meeting(registry, meeting, root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ArchiveOut.model_validate(archive)

    @app.get("/api/projects/{slug}/archives")
    def list_project_archives(slug: str) -> list[ArchiveOut]:
        lookup.project(registry, slug)
        archives = [
            archive
            for meeting in registry.list_meetings(slug)
            for archive in registry.list_archives(meeting.id)
        ]
        archives.sort(key=lambda archive: archive.id, reverse=True)
        return [ArchiveOut.model_validate(archive) for archive in archives]

    @app.get("/api/meetings/{meeting_id}/archives")
    def list_meeting_archives(meeting_id: int) -> list[ArchiveOut]:
        lookup.meeting(registry, meeting_id)
        return [
            ArchiveOut.model_validate(archive)
            for archive in registry.list_archives(meeting_id)
        ]

    @app.post("/api/archives/{archive_id}/verify")
    def post_verify(archive_id: int) -> ArchiveVerification:
        archive = lookup.archive(registry, archive_id)
        try:
            return verify_archive(archive.root_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/shutdown", status_code=202)
    def shutdown() -> ShutdownOut:
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
        return ShutdownOut(status="stopping")

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
    handed to a child (the design decision for ``--tailscale``). ``None`` keeps
    the
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
