"""The console's view seam: one named function per context a page renders.

Building a page's context used to be the inner working of one enormous factory.
It is now a call to this module: a builder takes what its context needs — the
**registry**, the **run manager**, the **request's locale**, the row or the id it
is about — and returns the context a template renders. Nothing here takes a
``Request``, so a handler reads as wiring — find the thing (``web/lookup.py``),
build the context, render it — and a test can build a context with no request at
all.

The **locale** is a real input, not decoration: ``tr`` reads the catalog the
*process* installed (``docs/i18n.md``), and the request middleware installed this
request's own choice before any route ran. A **surface context** — the function a
route asks for — takes the locale and *speaks* it (:func:`_speaks`), which is a
no-op in the request path and what makes the context *and the template render
that follows* a function of the request's locale rather than of whatever the
process last spoke; a caller with no request (a test building a context
directly) gets the locale it named. A **row builder** takes the locale when it
renders a translated string into its own context — an upload guard's reason, the
chip's label — and not otherwise; :func:`_auto_view` renders the resolver's
explanation without one, because the surface context that calls it has spoken.

One group of names is read from :mod:`clear_record.web.app` rather than imported
(:func:`_adapters`): the process probes the console binds there, and which the
console's own tests replace *there*. Reading the module that binds them is the
honest seam: a builder with its own import of a probe would quietly stop
honouring those patches, and the console's existing tests — which replace a
module-level name to pin a probe — would go on passing while testing nothing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from clear_record.core import (
    PROFILE_CUSTOM,
    PROFILES,
    JobEvent,
    PipelineOptions,
    profile_values,
    resolve_options,
)
from clear_record.core import i18n
from clear_record.core.i18n import deferred, tr
from clear_record.core.paths import (
    config_path,
    resolve_data_dir,
    resolve_logs_dir,
    resolve_models_dir,
    resolve_state_dir,
)
from clear_record.service import (
    BACKEND_AUTO,
    TERM_STATUSES,
    MalformedRunOptions,
    Meeting,
    MeetingAgent,
    PipelineRun,
    Registry,
    RunManager,
    RunState,
    Shape,
    cost_of,
    describe_draft,
    estimate_eta_s,
    managed,
    read_transcript,
    run_axes,
)
from clear_record.service.archive import ArchiveVerification, tool_version
from clear_record.service.auto import DEFAULT_MODEL, MODEL_LADDER
from clear_record.service.auto import render_message as render_service_message
from clear_record.service.diagnostics import machine_description

# The run vocabulary is read from the module that declares it, and only from
# there: three of these seven are also re-exported by the ``clear_record.service``
# façade, and taking some through each door leaves "where does the console read a
# run state?" with two answers.
from clear_record.service.lifecycle import (
    ACTIVE_RUN_STATUSES,
    ATTENTION_STATUSES,
    FAILED,
    QUEUED,
    RESUMABLE_STATUSES,
    RUNNING,
    TERMINAL_STATUSES,
)
from clear_record.service.runs import number_or_none
from clear_record.service.setup import (
    PI_AGENT,
    current_version,
    mcp_server_entry,
    read_setup_state,
    seen_version,
    setup_incomplete,
)
from clear_record.service.webhooks import WebhookStatus
from clear_record.web import lookup

#: The Settings page's sections (ADR-0027): each is a real URL,
#: ``/settings/<slug>``. This is the control plane's spine, not the project
#: navigation. The pinned sections: each is one job at a time.
#: Agent and MCP write the config; Status writes the setup marker (walk setup
#: again) and Models can download a checkpoint. The rest show their config path.
#: Each entry is ``(slug, label, template)``; the template is the partial
#: ``settings.html`` includes for that section, so the slug -> template mapping
#: lives here once rather than as an if/elif in the page.
#: The labels are ``deferred`` so Babel extracts them here while the template's
#: ``tr`` picks the *request's* locale, not the import-time one.
SETTINGS_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("models", deferred("Models"), "_settings_models.html"),
    ("backends", deferred("Backends"), "_settings_backends.html"),
    ("agent", deferred("Agent"), "_settings_agent.html"),
    ("mcp", deferred("MCP"), "_settings_mcp.html"),
    ("webhooks", deferred("Webhooks"), "_settings_webhooks.html"),
    ("storage", deferred("Storage"), "_settings_storage.html"),
    ("status", deferred("Status"), "_settings_status.html"),
)


#: The header chip's labels for the **in-flight** statuses, keyed by the status
#: itself and ordered by the precedence the chip reports them in: a node executing
#: work outranks one waiting for it, so :data:`RUNNING` comes first. The chip reads
#: :data:`ACTIVE_RUN_STATUSES` for *which* statuses are in flight — the one
#: declaration of that pair — and this table only names and orders them; the
#: declaration's own order is the lifecycle's (:data:`QUEUED` before
#: :data:`RUNNING`), not a display order, so it is not what the chip ranks by.
#: ``tests/web/test_web_activity.py`` asserts the table covers the declaration
#: exactly, so a third in-flight status cannot arrive without a label and a place.
#: The labels are ``deferred`` because Babel extracts message IDs from
#: ``tr``/``deferred`` call sites only, and the lookup here is a
#: ``tr(ACTIVE_RUN_LABELS[status], …)``.
ACTIVE_RUN_LABELS: dict[str, str] = {
    RUNNING: deferred("running {count}"),
    QUEUED: deferred("queued {count}"),
}

#: How many of the newest meetings the Projects landing's activity line and a
#: project's Overview show. Both are cheap registry reads; walking every
#: workspace for tapes and transcripts is the project's Media tab's job.
RECENT_MEETINGS = 5

#: How many finished runs the Activity page lists. The page answers
#: "what is clear-record doing right now": the live queue is complete, and the
#: history is the newest outcomes — a meeting's whole run history is its own
#: page, not this one.
ACTIVITY_HISTORY = 20

#: How many transcript segments the meeting view shows per page. The transcript
#: is paged rather than rendered whole: a multi-hour tape is tens of thousands of
#: ``HH:MM:SS.mmm [speaker] text`` lines, which no reviewer reads in one scroll.
TRANSCRIPT_PAGE = 500

#: The stage whose units are audio chunks — the economy the cost record's
#: ``chunks``/``chunk_seconds`` pair measures. It is the only stage a
#: live rate can be read from: every other stage's units are something else.
_CHUNK_STAGE = "transcribe"


# --------------------------------------------------------------------------- #
# the console's own bindings, and the locale a build speaks
# --------------------------------------------------------------------------- #
def _adapters():
    """The console module — where this console binds the process's adapters.

    A handful of the facts these builders render are module-level names of
    :mod:`clear_record.web.app`: this machine's ASR backends
    (``available_backend_ids``), the checkpoints on disk (``models_on_disk``), an
    archive's verification (``verify_archive``), each backend's availability
    (``backend_status``), and the guided agent setup's own reads (``setup_view``,
    ``find_harness``). The console's tests replace them **there** — all six, each
    pinned on ``clear_record.web.app`` — the module that *binds* them, not where
    they are declared: ``available_backend_ids`` in ``tests/web/conftest.py``
    (the probe
    that would otherwise compile and run the ASR helper) and
    ``tests/web/test_web_api.py``, ``verify_archive`` in
    ``tests/web/test_web_api.py``, ``models_on_disk`` and ``backend_status`` in
    ``tests/web/test_web_settings.py``, and ``setup_view`` and ``find_harness``
    in ``tests/web/test_web_agent_setup.py`` — so a builder that read its own
    import of them would quietly stop honouring those patches. Deferred, because
    that module imports this one.
    """
    from clear_record.web import app as console

    return console


def _speaks(locale: str) -> None:
    """Render the strings this build produces in ``locale``.

    ``tr`` reads whatever catalog the process installed, and the console's
    request middleware installed the request's own choice before any route ran —
    so a context built for a request already speaks its locale and nothing
    happens here. The call is what makes a build a function of its locale rather
    than of whatever the process last spoke, which is what lets a caller with no
    request (a test building a context directly) get the locale it named.
    """
    name = None if locale == i18n.SOURCE_LOCALE else locale
    if i18n.current_locale() != name:
        i18n.install(locale)


# --------------------------------------------------------------------------- #
# the console's own vocabulary (the Settings sections, the run form's choices)
# --------------------------------------------------------------------------- #
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


#: The picker's choices, derived from the shared ``core`` table (never restated),
#: with ``custom`` — "no preset" — last. A profile added to ``PROFILES`` shows up
#: in the picker with no web change, so the console cannot drift from the CLI.
PROFILE_CHOICES = tuple(name for name in PROFILES if name != PROFILE_CUSTOM) + (
    PROFILE_CUSTOM,
)


def run_backend_choices() -> tuple[str, ...]:
    """The run form's backend ids: what this machine can run, then ``auto``.

    Availability is the service's own probe (``service.auto``'s
    ``available_backend_ids``, re-exported from the providers), so the picker
    offers ``apple-speech`` where it exists and never offers a backend this
    machine cannot run — the same set ``--backend auto`` chooses from. The
    capability-driven ``auto`` sentinel stays last (never the default).
    """
    return _adapters().available_backend_ids() + (BACKEND_AUTO,)


# --------------------------------------------------------------------------- #
# one run, as a fragment or a row renders it
# --------------------------------------------------------------------------- #
def _auto_view(meta: dict | None) -> dict:
    """The run-explanation keys for the run fragment.

    ``profile`` / ``knobs`` are the **resolved** values the run meta recorded;
    ``explanations`` is the resolver's own wording, reused verbatim, for whichever
    of ``--backend auto`` / ``--auto`` ran (backend first, the resolver's order).
    The meta records that wording as a stable ID+parameters (``message``); the
    console renders it with ``tr`` here, so the explanation is translated even
    though the pipeline composed it. A run recorded before ``message`` existed falls
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


def _run_axes(row, meeting) -> dict | None:
    """The four axes for a **terminal** run, else ``None``.

    The axes are workspace reads (the record and the transcript meta), so a
    live run -- which has no cost record yet -- is not measured: the axes
    appear when the run stops, and while it runs the fragment stays a progress
    view. This is the only place the console keeps the axes as a dict for a
    template to render; :func:`run_row` asks for them too and reads only
    ``speed``.
    """
    if row is None or row.status not in TERMINAL_STATUSES:
        return None
    return run_axes(row, directory=meeting.workspace_path if meeting else None)


def _run_machine(run: PipelineRun) -> str | None:
    """The machine a run is on, from what was *recorded*, never a guess.

    A run that stopped carries the machine description its cost record
    measured; a run still in flight carries the host its claiming owner named. A
    queued run has neither — nothing has claimed it yet — so the column says
    unknown rather than naming this node, which the claim has not yet agreed
    to.
    """
    machine = cost_of(run).get("machine")
    if isinstance(machine, str) and machine:
        return machine
    host, sep, _pid = (run.owner or "").rpartition(":")
    return host if sep and host else None


def _run_speed_so_far(run: PipelineRun, event: JobEvent | None) -> float | None:
    """A **running** run's rate so far, in audio seconds per wall second.

    The rate *so far*, not the run's average, and it is derived from the run's
    own persisted primitives: the chunks its last transcribe event reported
    that the decoder actually **ran**, times the chunk length its queued options
    resolved, over that stage's own elapsed seconds — so the numerator and the
    denominator cover the same work. The terminal cost record's speed is the
    whole run's audio over its whole wall clock, including ingest and
    reconcile; this is what the decoder is sustaining now, which is why the page
    labels it "so far".

    ``reused`` is subtracted from the count, not ignored: a resumed run serves
    chunks from the cache, those advance the stage without the decoder touching
    them (``Progress.advance(reused=True)``), and the elapsed clock covers only
    the chunks it did decode. Without that, a run with 112 of 120 chunks cached
    would report the fixture's 260x instead of its ~2.4x.

    It is chunk-granular: the chunk in flight is counted when its event lands,
    and overlapping chunks count at their full length, so it reads a little
    high. ``None`` is the honest answer whenever a primitive is missing — a
    queued run, a run whose last event belongs to another stage, a run enqueued
    without a chunk plan, an event with no elapsed time yet, a run whose chunks
    were all re-used (nothing was decoded to rate), or arithmetic that would
    divide by zero — and the page says unknown.
    """
    if run.status != RUNNING or event is None or event.stage != _CHUNK_STAGE:
        return None
    options = run.run_options if isinstance(run.run_options, dict) else {}
    chunk_seconds = number_or_none(options.get("chunk_seconds"))
    if chunk_seconds is None or event.elapsed_s is None:
        return None
    audio = (event.index - event.reused) * chunk_seconds
    if audio <= 0 or event.elapsed_s <= 0:
        return None
    return round(audio / event.elapsed_s, 3)


def _run_controls(row: PipelineRun | None, *, status: str) -> dict:
    """The cancel/resume controls a run fragment shows.

    Derived from the row, never from live process state: a queued or running run
    can be cancelled, a terminal one that did not finish can be resumed, and a
    resume says which run it continues.
    """
    return {
        "cancellable": status in ACTIVE_RUN_STATUSES,
        "cancel_requested": bool(row.cancel_requested_at) if row is not None else False,
        "resumable": status in RESUMABLE_STATUSES,
        "resumes_run_id": row.resumes_run_id if row is not None else None,
    }


def _run_context(
    state: RunState | None,
    *,
    meeting_id: int,
    fallback=None,
    meta: dict | None = None,
    history_eta_s: float | None = None,
    axes: dict | None = None,
    row: PipelineRun | None = None,
) -> dict:
    """The template context for one run fragment (live state, else the last row).

    ``row`` is the registry row behind the fragment. The run's control state — can
    it be cancelled, was it asked to stop, can it be resumed, what does it
    resume — lives there rather than in the live state: it is what survives a
    restart, and it is what another writer (the agent's MCP server) can change.

    ``history_eta_s`` is the service's history-based estimate. When a
    matching history produced one it replaces the stage-local estimate, which
    stays the live fallback — and the only display — otherwise.

    ``axes`` is the service's four-axis dict, present only for a
    terminal run; the template renders it verbatim.
    """
    if state is not None:
        context = state.summary().model_dump()
        last = state.last
        context["meeting_id"] = state.meeting_id
        context["message"] = last.message if last else ""
        context["polling"] = state.status in ACTIVE_RUN_STATUSES
        context["axes"] = axes
        context.update(_run_controls(row, status=state.status))
        if history_eta_s is not None:
            context["eta_s"] = history_eta_s
        context.update(_auto_view(meta))
        return context
    context = {
        "run_id": fallback.id,
        "meeting_id": meeting_id,
        "status": fallback.status,
        "stage": None,
        "index": 0,
        "total": 0,
        "eta_s": history_eta_s,
        "message": "",
        "error": fallback.error,
        "polling": False,
        "axes": axes,
    }
    context.update(_run_controls(row, status=fallback.status))
    context.update(_auto_view(meta if meta is not None else fallback.options))
    return context


def run_context(
    registry: Registry, runs: RunManager, locale: str, state: RunState
) -> dict:
    """One run's live fragment: the state, its row and everything derived from them.

    Every read the fragment needs is here rather than at the route: the row the
    control state lives on, the meeting whose workspace the axes are measured in,
    the service's history-based estimate. A route that renders a run
    fragment is therefore one call.
    """
    _speaks(locale)
    row = registry.get_run(state.run_id)
    meeting = registry.meeting_by_id(state.meeting_id)
    return _run_context(
        state,
        meeting_id=state.meeting_id,
        meta=row.options if row else None,
        history_eta_s=estimate_eta_s(registry, row) if row else None,
        axes=_run_axes(row, meeting),
        row=row,
    )


def run_refusal_context(meeting_id: int, message: str) -> dict:
    """A run the service refused before one existed, as the fragment shows it."""
    # A refusal, not a run: no run id exists yet, so there is nothing to
    # project an estimate for and no record to read.
    return {
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


def run_row(registry: Registry, run: PipelineRun, projects: Mapping[str, str]) -> dict:
    """One run as the Activity page renders it, live or finished.

    Every field comes from a pinned source: the run's own columns, its cost
    record through the service's axes, and — for a run still in
    flight — its newest persisted progress event, the rate that event
    supports and the service's history-based ETA. Nothing is invented for a
    run that has not recorded it: a run with no cost record has no recorded
    speed, and a run that is not transcribing has no rate so far, so both
    read unknown rather than being filled in from a second quantity.
    """
    meeting = registry.meeting_by_id(run.meeting_id)
    slug = meeting.project_slug if meeting else ""
    recorded = run_axes(run)["speed"]
    live = run.status in ACTIVE_RUN_STATUSES
    event = registry.latest_run_event(run.id) if live else None
    eta_s = event.eta_s if event else None
    if run.status == RUNNING:
        # Not `ACTIVE_RUN_STATUSES`: a queued run has not started, and the
        # history-based estimate is about a rate this node is currently
        # sustaining. It replaces the stage-local one when this machine has a
        # matching history, the same precedence the run fragment
        # applies.
        history_eta_s = estimate_eta_s(registry, run)
        if history_eta_s is not None:
            eta_s = history_eta_s
    return {
        "run": run,
        # Which fragment the row is: the run is in flight (ACTIVE_RUN_STATUSES),
        # so `_activity_run.html` reads the one classification, not its own copy.
        "live": live,
        "project_slug": slug,
        "project_name": projects.get(slug, slug),
        "meeting_slug": meeting.slug if meeting else "",
        "meeting_title": meeting.title if meeting else "",
        # A queued run's place in the node's FIFO; 0 for every other status.
        "position": registry.queue_position(run.id),
        "stage": event.stage if event else None,
        "index": event.index if event else 0,
        "total": event.total if event else 0,
        "eta_s": eta_s,
        # Two different quantities, kept apart: the speed a finished run's
        # cost record measured, and the rate a running run is sustaining so
        # far. A row has at most one of them.
        "speed": recorded["x_realtime"],
        "speed_so_far": _run_speed_so_far(run, event),
        "wall_s": recorded["wall_seconds"],
        "machine": _run_machine(run),
    }


# --------------------------------------------------------------------------- #
# the shared views both surfaces render
# --------------------------------------------------------------------------- #
def error_message(locale: str, exc: BaseException) -> str:
    """One user-facing error, translated when the service supplied a message ID.

    A service error that carries ``msgid``/``params`` (``managed.UploadRejected``,
    ``MeetingAgentError``) is rendered through ``tr`` here, at the presentation
    boundary; anything else is its own English diagnostic, passed through
    unchanged rather than restating a rule the service owns.
    """
    _speaks(locale)
    msgid = getattr(exc, "msgid", None)
    if isinstance(msgid, str):
        return tr(msgid, **getattr(exc, "params", {}))
    return str(exc)


# --- webhooks: delivery health (ADR-0020) ---------------------------------- #
# The label for each overall webhook state. Each branch calls ``tr`` with a
# literal so the catalog tooling can extract it; a state the service adds *later*
# falls through to the neutral "Configured", and so does today's ``ok`` — so
# neither vanishes from display.
def _webhook_state_label(state: str) -> str:
    if state == "not_configured":
        return tr("Not configured")
    if state == "config_problem":
        return tr("Configuration problem")
    if state == "delivery_failed":
        return tr("A delivery is failing")
    if state == "no_delivery_yet":
        return tr("No delivery yet")
    return tr("Configured")


def _webhook_health_label(health: str) -> str:
    if health == "config_problem":
        return tr("Config problem")
    if health == "delivery_failed":
        return tr("Failing")
    if health == "delivered":
        return tr("Delivered")
    return tr("No delivery yet")


class WebhookDeliveryOut(Shape):
    """One endpoint's newest delivery attempt (ADR-0020)."""

    outcome: str
    outcome_label: str
    at: str
    attempts: int
    http_status: int | None
    error: str | None
    event_type: str
    event_id: str


class WebhookEndpointOut(Shape):
    """One configured endpoint: its config facts, and its last delivery."""

    name: str | None
    url: str
    events: list[str]
    include_content: bool
    signed: bool
    health: str
    health_label: str
    problems: list[str]
    last_delivery: WebhookDeliveryOut | None


class WebhookStatusOut(Shape):
    """`/api/webhooks`: the overall state, the config problems, one row per endpoint.

    The signing secret is nowhere in this shape — only whether an endpoint is
    signed — and a problem names the environment variable, never its value.
    """

    state: str
    state_label: str
    configured: bool
    problems: list[str]
    endpoints: list[WebhookEndpointOut]


def webhook_status_view(locale: str, status: WebhookStatus) -> WebhookStatusOut:
    """The console's webhook view: overall state, config problems, per-endpoint last delivery.

    Built field by field rather than by ``dataclasses.asdict`` on purpose: the
    endpoint view names only what a reader needs, so no field that could ever
    carry secret material can leak into a template or the JSON API by accident.
    The signing secret is read from the environment at delivery time and never
    stored (ADR-0020); the config problems are the emitter's own strings, passed
    through verbatim — a runtime string with a path in it, so ``tr`` was a no-op —
    and never re-derived, and even those name the *environment variable*, never
    its value. The endpoint carries only *whether* it is signed.

    ``state`` comes from :attr:`WebhookStatus.state`, so "not configured",
    "config broken" and "delivery failing" stay distinct.
    """
    _speaks(locale)
    endpoints = []
    for report in status.endpoints:
        last = report.last_delivery
        endpoints.append(
            WebhookEndpointOut(
                name=report.name,
                url=report.url,
                events=list(report.events),
                include_content=report.include_content,
                signed=report.signed,
                health=report.health,
                health_label=_webhook_health_label(report.health),
                problems=list(report.problems),
                last_delivery=None
                if last is None
                else WebhookDeliveryOut(
                    outcome="delivered" if last.status == "delivered" else "failed",
                    outcome_label=tr("Delivered")
                    if last.status == "delivered"
                    else tr("Failed"),
                    at=last.at,
                    attempts=last.attempts,
                    http_status=last.http_status,
                    error=last.error,
                    event_type=last.type,
                    event_id=last.event_id,
                ),
            )
        )
    return WebhookStatusOut(
        state=status.state,
        state_label=_webhook_state_label(status.state),
        configured=bool(status.endpoints),
        problems=list(status.problems),
        endpoints=endpoints,
    )


# --------------------------------------------------------------------------- #
# the rows a page lists
# --------------------------------------------------------------------------- #
def project_rows(registry: Registry) -> list[dict]:
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


def recent_meetings(registry: Registry, projects: list[dict]) -> list[dict]:
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


def meeting_rows(
    registry: Registry, runs: RunManager, locale: str, slug: str
) -> list[dict]:
    """Every meeting of one project as the Meetings tab lists it."""
    _speaks(locale)
    rows: list[dict] = []
    for meeting in registry.list_meetings(slug):
        tape_set = registry.latest_recording_set(meeting.id)
        latest = registry.list_runs(meeting.id)
        state = runs.state(latest[0].id) if latest else None
        eta_s = estimate_eta_s(registry, latest[0]) if latest else None
        axes = _run_axes(latest[0], meeting) if latest else None
        if state is not None:
            run = _run_context(
                state,
                meeting_id=meeting.id,
                meta=latest[0].options,
                history_eta_s=eta_s,
                axes=axes,
                row=latest[0],
            )
        elif latest:
            run = _run_context(
                None,
                meeting_id=meeting.id,
                fallback=latest[0],
                meta=latest[0].options,
                history_eta_s=eta_s,
                axes=axes,
                row=latest[0],
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


def media_rows(registry: Registry, slug: str) -> list[dict]:
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
                "workspace_path": usage.workspace_path,
                "managed": usage.managed,
                "tapes": [
                    {
                        "id": tape.id,
                        "name": tape.name,
                        "path": tape.path,
                        "sha256": tape.sha256,
                        "sha256_short": tape.sha256[:12],
                        "bytes": tape.bytes,
                        "size": human_bytes(tape.bytes),
                    }
                    for tape in usage.tapes
                ],
                "transcript": None
                if transcript is None
                else {"segments": transcript.total, "source": transcript.source},
            }
        )
    return rows


def archive_status(archive) -> ArchiveVerification | None:
    """The verification summary, or None when the manifest is gone."""
    try:
        return _adapters().verify_archive(archive.root_path)
    except FileNotFoundError:
        return None


def archive_rows(registry: Registry, slug: str) -> list[dict]:
    rows: list[dict] = []
    for meeting in registry.list_meetings(slug):
        for archive in registry.list_archives(meeting.id):
            rows.append({"archive": archive, "meeting": meeting})
    rows.sort(key=lambda row: row["archive"].id, reverse=True)
    return rows


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


def artifact_text(artifact) -> str | None:
    """An artifact's file contents, or ``None`` when the file is gone."""
    try:
        return Path(artifact.path).read_text(encoding="utf-8")
    except OSError:
        return None


def minutes_rows(registry: Registry, slug: str) -> list[dict]:
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
        heading = next((line.strip() for line in text.splitlines() if line.strip()), "")
        rows.append(
            {"meeting": meeting, "artifact": artifact, "heading": heading[:120]}
        )
    rows.sort(key=lambda row: row["artifact"].id, reverse=True)
    return rows


def backend_rows() -> list[dict]:
    """Each ASR backend's availability and its message node, in catalog order.

    diagnostics.backend_status is the service's one probe (it reaches providers
    through the pipeline layer, which the web layer may not import); the console
    only renders the verdict and the reason, never recomputes them. The
    reason is a :class:`~clear_record.core.message.Message` JSON node, and
    the shared template renders it with `tr` at the boundary.
    """
    return [
        {
            "id": backend_id,
            "available": verdict["available"],
            "reason": verdict["reason"],
        }
        for backend_id, verdict in _adapters().backend_status().items()
    ]


# --------------------------------------------------------------------------- #
# the pages' contexts
# --------------------------------------------------------------------------- #
def project_context(
    registry: Registry,
    runs: RunManager,
    locale: str,
    slug: str,
    tab: str = "overview",
    error: str | None = None,
) -> dict:
    """Everything one project sub-tab renders, from the service only.

    Each tab loads only what it shows, so a render (or a mutation's
    re-render) never pays for the surfaces the operator is not looking at.
    Raises the one lookup rule's ``NotFound`` for an unknown slug, so each
    caller owns its answer: the fragment route lets the app's handler answer it
    with a 404 body (htmx does not swap it, so the pane keeps what it shows),
    while the page route renders the not-found page.
    """
    _speaks(locale)
    project = lookup.project(registry, slug)
    context: dict = {"project": project, "tab": tab, "error": error}
    if tab == "meetings":
        context.update(
            meetings=meeting_rows(registry, runs, locale, slug),
            backends=run_backend_choices(),
            profiles=PROFILE_CHOICES,
            profile_default=PROFILE_CUSTOM,
            profile_options=profile_preview(PROFILE_CUSTOM),
            archives=archive_rows(registry, slug),
        )
    elif tab == "glossary":
        context.update(terms=registry.list_terms(slug), statuses=TERM_STATUSES)
    elif tab == "media":
        context["media"] = media_rows(registry, slug)
    else:
        meetings = registry.list_meetings(slug)
        context.update(
            meeting_count=len(meetings),
            term_count=len(registry.list_terms(slug)),
            recent=[
                {"meeting": meeting, "managed": managed.is_managed(meeting)}
                for meeting in meetings[:RECENT_MEETINGS]
            ],
            minutes=minutes_rows(registry, slug),
        )
    return context


def meeting_context(
    registry: Registry,
    runs: RunManager,
    locale: str,
    meeting: Meeting,
    *,
    agent: MeetingAgent,
    offset: int = 0,
    error: str | None = None,
) -> dict:
    """Everything the meeting review view renders, from the service only.

    The transcript is the same :func:`read_transcript` the MCP adapter uses
    (paged here), the artifacts are the registry's rows, and the drafts are
    read back from the workspace with
    :meth:`~clear_record.service.MeetingAgent.drafts`, so a harness's result and
    its author provenance are shown rather than re-derived.

    ``agent`` is the app's injected draft seam; constructing it from the meeting
    is wiring, and a view may not do it.
    """
    _speaks(locale)
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
        "drafts": [describe_draft(draft).model_dump() for draft in agent.drafts()],
        "legacy_drafts": len(agent.legacy_drafts()),
        "minutes_artifact": minutes_artifact,
        "minutes_text": artifact_text(minutes_artifact)
        if minutes_artifact is not None
        else None,
        "error": error,
    }


def upload_error_label(locale: str, exc: managed.UploadRejected) -> str:
    """A short, translated label for one upload guard.

    The *reason* is always the service's own message, rendered beside this,
    so the web layer never restates a guard's rule or its facts.
    """
    _speaks(locale)
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


def refusal(locale: str, exc: managed.UploadRejected, label: str) -> dict:
    """A guard failure as the panel shows it: a translated label + the
    service's message.

    The reason is the service's own message ID, rendered here with ``tr`` at
    the presentation boundary — one rule, one home: the guard that enforces
    the rule supplies the ID and its facts, and the console only translates.
    """
    _speaks(locale)
    return {"label": label, "reason": tr(exc.msgid, **exc.params)}


def delete_refusal(locale: str, exc: managed.UploadRejected) -> dict:
    """A refused tape delete, in the service's own words.

    The archive is the durable copy, so the console's *label* for the refusal is
    its own; the reason is still the service's message, as
    :func:`refusal` renders it.
    """
    _speaks(locale)
    return refusal(locale, exc, tr("Delete refused"))


def upload_refusal(registry: Registry, locale: str, meeting: Meeting) -> dict | None:
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
        return refusal(locale, exc, tr("This meeting cannot take an upload"))
    return None  # pragma: no cover - is_managed / no-path are handled above


def storage_context(registry: Registry, locale: str, meeting: Meeting) -> dict:
    """The template context for one meeting's storage panel (ADR-0024).

    Everything factual is the service's: the resolved workspace path, the
    workspace and tape sizes, the managed root, free space on it and the
    tapes. Free space is ``usage.free_bytes`` — the **same**
    ``root_free_bytes`` the upload guard checks, so the panel and the guard
    can never disagree about the disk. Formatting it for a human is the only
    thing this view does with it.
    """
    _speaks(locale)
    usage = managed.meeting_storage(registry, meeting)
    free_bytes = usage.free_bytes
    tapes = [
        {
            "id": tape.id,
            "name": tape.name,
            "path": tape.path,
            "sha256": tape.sha256,
            "sha256_short": tape.sha256[:12],
            "bytes": tape.bytes,
            "size": human_bytes(tape.bytes),
        }
        for tape in usage.tapes
    ]
    tapes_bytes = sum(tape["bytes"] for tape in tapes)
    return {
        "meeting_id": meeting.id,
        "workspace_path": usage.workspace_path,
        "managed": usage.managed,
        # A managed meeting uploads; so does one with no workspace yet (the
        # first upload provisions a managed one). A user-chosen one cannot.
        "can_upload": usage.managed or not usage.workspace_path,
        "managed_root": usage.managed_root,
        "meeting_bytes": usage.bytes,
        "meeting_size": human_bytes(usage.bytes),
        "tapes_bytes": tapes_bytes,
        "tapes_size": human_bytes(tapes_bytes),
        "free_bytes": free_bytes,
        "free_size": None if free_bytes is None else human_bytes(free_bytes),
        "tapes": tapes,
        "upload_refusal": upload_refusal(registry, locale, meeting),
    }


def chip_context(registry: Registry, locale: str) -> dict:
    """The header chip's live state, read from the same registry.

    The chip answers "what is this node doing right now" and must not lie:
    a run in flight makes it say so (with the queue's depth when the node
    has not picked the work up yet), and with nothing in flight the **newest
    finished** run — the one that ended most recently, not the row written
    last — decides between "needs attention" (its status is one of
    :data:`~clear_record.service.lifecycle.ATTENTION_STATUSES`) and idle. A
    ``stopped`` run was cancelled on purpose, so it is not attention. The
    label is translated at render time.

    Which statuses are in flight is :data:`ACTIVE_RUN_STATUSES` and not a
    second list here; the label table's order is the one the chip reports in,
    because the declaration's order is the lifecycle's (``queued`` is written
    before ``running``) and this chip speaks about now.
    """
    _speaks(locale)
    active = registry.runs_with_status(*ACTIVE_RUN_STATUSES)
    if active:
        counts = Counter(run.status for run in active)
        status = next(name for name in ACTIVE_RUN_LABELS if counts.get(name))
        return {
            "label": tr(ACTIVE_RUN_LABELS[status], count=counts[status]),
            "state": status,
        }
    newest = registry.finished_runs(*TERMINAL_STATUSES, limit=1)
    if newest and newest[0].status in ATTENTION_STATUSES:
        return {"label": tr("needs attention"), "state": FAILED}
    return {"label": tr("idle"), "state": "neutral"}


def activity_context(registry: Registry, locale: str) -> dict:
    """The pipeline status page: one node's runs, live and recent.

    Running and queued runs come from the **shared** registry, across every
    project, so a run an agent's MCP server started is here with
    the same fields as one the console started — including the
    origin it was started from. This is one node's view: the rows carry the
    machine that measured or claimed them, and there is no aggregation
    across nodes (a non-goal).
    """
    _speaks(locale)
    projects = {row.slug: row.name for row in registry.list_projects()}
    history = registry.finished_runs(*TERMINAL_STATUSES, limit=ACTIVITY_HISTORY)
    return {
        "live": [
            run_row(registry, run, projects)
            for run in registry.runs_with_status(*ACTIVE_RUN_STATUSES)
        ],
        # Newest **finish** first, the same ranking the chip uses: the page
        # leads with the latest outcome, and it is the row the chip is
        # talking about.
        "history": [run_row(registry, run, projects) for run in history],
        # Which node this page is about: the same description a run's cost
        # record stores, so the subtitle and a row's machine
        # speak one language.
        "machine": machine_description(),
    }


def models_context(locale: str) -> dict:
    """The transcription defaults: model, language, models dir, profiles.

    models_on_disk and DEFAULT_MODEL are the service's own reads
    (re-exported from pipeline.auto), and profile_preview is the same resolver
    the run form previews, so the page cannot disagree with a run.
    """
    _speaks(locale)
    return {
        "default_model": DEFAULT_MODEL,
        "default_language": "auto",
        "models_dir": str(resolve_models_dir()),
        "models_present": sorted(_adapters().models_on_disk()),
        # The sizes the picker offers; the template marks which are on disk,
        # so choosing a different model is one glance and one click.
        "model_ladder": MODEL_LADDER,
        "profiles": [profile_preview(name) for name in PROFILE_CHOICES],
        "config_path": str(config_path()),
    }


def storage_settings_context(registry: Registry, locale: str) -> dict:
    """The machine total, the managed root, its free space, each archive root."""
    _speaks(locale)
    root = managed.managed_root()
    try:
        free_size = human_bytes(managed.root_free_bytes(root))
    except OSError:
        free_size = None
    return {
        "machine_storage": managed.machine_storage(registry, root),
        "managed_root": str(root),
        "free_size": free_size,
        "archive_roots": [
            {"project": project, "root": project.default_archive_root}
            for project in registry.list_projects()
        ],
        "config_path": str(config_path()),
    }


def status_context(registry: Registry, locale: str) -> dict:
    """Version, the resolved directories, the queue and backend availability."""
    _speaks(locale)
    queue = []
    for run in registry.runs_with_status(*ACTIVE_RUN_STATUSES):
        meeting = registry.meeting_by_id(run.meeting_id)
        queue.append(
            {
                "run": run,
                "project_slug": meeting.project_slug if meeting else "",
                "meeting_title": meeting.title if meeting else "",
                "position": registry.queue_position(run.id),
            }
        )
    return {
        "version": tool_version(),
        "dirs": [
            {"label": tr("Data"), "path": str(resolve_data_dir())},
            {"label": tr("State"), "path": str(resolve_state_dir())},
            {"label": tr("Logs"), "path": str(resolve_logs_dir())},
            {"label": tr("Config"), "path": str(config_path())},
            {"label": tr("Models"), "path": str(resolve_models_dir())},
        ],
        "queue": queue,
        "backends": backend_rows(),
        "config_path": str(config_path()),
        # The permanent hello-world check renders its idle state here; the
        # /ui/hello-check POST swaps a result into #hello-check.
        "check": None,
    }


def settings_context(registry: Registry, locale: str, section: str) -> dict:
    """Everything one Settings section renders, from the service only.

    Only the active section's reads run, so opening Settings never probes
    what the operator is not looking at (agent, MCP and webhooks load their
    panels as htmx fragments).
    """
    entry = lookup.settings_section(SETTINGS_SECTIONS, section)
    _speaks(locale)
    context: dict = {
        "section": section,
        "settings_sections": SETTINGS_SECTIONS,
        "section_label": entry[1],
        "section_template": entry[2],
    }
    if section == "models":
        context.update(models_context(locale))
    elif section == "backends":
        context["backends"] = backend_rows()
        context["config_path"] = str(config_path())
    elif section == "webhooks":
        context["config_path"] = str(config_path())
    elif section == "storage":
        context.update(storage_settings_context(registry, locale))
    elif section == "status":
        context.update(status_context(registry, locale))
    return context


def unreadable_row_context(locale: str, exc: MalformedRunOptions) -> dict:
    """The 409 page's context: a stored run row this build cannot read.

    The refusal is nobody's request, so the page is a page of its own rather than
    the console's chrome — which is what the app's handler renders. The frame
    (which run, and that its stored options cannot be read) is a message ID
    rendered here in the reader's locale; the field detail ``render`` appends is
    pydantic's own words and has no catalog entry by design (``docs/i18n.md``).
    """
    _speaks(locale)
    return {"message": exc.render(tr)}


def setup_flags() -> dict:
    """The setup marker's page-level facts (ADR-0027).

    ``setup_incomplete`` shows the Setup link; ``setup_update`` shows the
    dismissible "updated" notice. Both read the one record the service owns,
    so the nav and the notice cannot disagree about the marker.
    """
    state = read_setup_state()
    current = current_version()
    seen = seen_version(state=state)
    return {
        "setup_incomplete": setup_incomplete(state=state, version=current),
        "setup_update": bool(seen) and seen != current,
        "current_version": current,
    }


def page_context(registry: Registry, locale: str, *, nav: str, **extra) -> dict:
    """The context a full page extending ``base.html`` renders.

    Every page needs the top-level nav item, the header chip's live state and
    the setup marker: the Setup link and the update notice both come from
    :func:`setup_flags`, so a page render cannot forget either.
    """
    _speaks(locale)
    return {
        "nav": nav,
        "chip": chip_context(registry, locale),
        **setup_flags(),
        **extra,
    }


def agent_setup_context(
    locale: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    part: str = "agent",
) -> dict:
    """The agent setup panel's context: the service's state, plus a step's result.

    Every fact is the service's own — the harness and MCP client config that are
    recorded, whether their paths are still there, and what the 0.2 agent
    configuration this version ignores. This boundary only renders and
    translates (the same split ``refusal()`` uses for an upload guard).
    """
    _speaks(locale)
    # part="mcp" renders the MCP rung alone; the default is the whole flow.
    standalone = part == "mcp"
    console = _adapters()
    return {
        "setup": console.setup_view(),
        # The default-named harness, looked up on PATH. It is a *hint* for
        # the "point at an existing pi-agent" rung, never a fallback: the
        # harness and its client config are both the user's choice.
        "harnesses": console.find_harness(),
        "pi_agent": PI_AGENT,
        "mcp_entry": mcp_server_entry(),
        "mcp_context": "mcp" if standalone else "agent",
        "error": error,
        "notice": notice,
        # No check has run on a plain render; the Try it stage shows its
        # idle state and the POST below swaps a result in.
        "check": None,
    }
