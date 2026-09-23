"""Opt-in outbound webhooks: POST an event to a user's own system.

A user can point clear-record at their knowledge base or project-management
system and be told when something happens here. The feature is **opt-in** —
with no endpoints configured nothing is sent — and **non-blocking**: delivery
runs on its own worker thread, so an unreachable endpoint, a timeout or a
non-2xx response can never fail or stall a pipeline run.

The boundaries, and why the defaults are what they are:

- **Content-free by default.** The receiver is usually a third party and a
  transcript is private (the spirit of ADR-0006), so the payload carries an
  event id, a type, a timestamp and the project/meeting/run ids — never
  transcript text. Content is an explicit **per-endpoint** opt-in
  (:attr:`WebhookEndpoint.include_content`); the producer supplies it through
  ``emit(..., content=...)`` and it is stripped for every endpoint that did not
  ask for it.
- **The signing secret lives in the environment, never the registry** (the BYOK
  rule: no credential is stored, ADR-0013/ADR-0017). ``secret_env`` names the
  variable; its value is read at
  delivery time and used for an HMAC-SHA256 signature header. An endpoint that
  names a secret env but finds it unset is recorded as failed and sent nothing:
  there is no silent unsigned downgrade.
- **Delivery is recorded, and readable.** Every delivery's outcome is kept in a
  bounded ring buffer and exposed through :meth:`WebhookEmitter.status` together
  with the endpoint's config problems, so a console can answer "is my endpoint
  configured, and did the last event arrive?" without ever seeing the secret.

Delivery uses the stdlib only (:mod:`urllib.request`), so the base install gains
no HTTP dependency.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import os
import queue
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from clear_record.core.paths import config_path
from clear_record.service.lifecycle import ANNOUNCEMENTS

# --- the event vocabulary -------------------------------------------------- #
#
# The **run** events this build emits are the run lifecycle's own: which move
# notifies and under what name is declared where the move is
# (`service/lifecycle.py` — `ANNOUNCEMENTS`, each `RunTransition.event` derived
# from the move's name), so a notifying move reaches a receiver's vocabulary by
# being declared and there is no second place to name it. Read one by name off
# the move itself (`lifecycle.FINISH.event`); a receiver filters on
# `EMITTED_EVENTS` or `ALL_EVENTS`.

# The events about everything else this build emits.
TRANSCRIPT_READY = "transcript.ready"
ARCHIVE_CREATED = "archive.created"

EMITTED_EVENTS: tuple[str, ...] = (
    *ANNOUNCEMENTS,
    TRANSCRIPT_READY,
    ARCHIVE_CREATED,
)

#: Reserved names for hooks whose producer does not exist yet: a glossary edit,
#: an agent-task draft and an accepted minutes document. Named here so the
#: vocabulary is stable and a receiver can filter on them early.
FUTURE_EVENTS: tuple[str, ...] = (
    "glossary.updated",
    "agent_task.draft",
    "minutes.accepted",
)

#: Every event name a config may filter on.
ALL_EVENTS: tuple[str, ...] = EMITTED_EVENTS + FUTURE_EVENTS

#: The header carrying the endpoint's HMAC-SHA256 signature (``sha256=<digest>``).
SIGNATURE_HEADER = "X-Clear-Record-Signature"
EVENT_HEADER = "X-Clear-Record-Event"
DELIVERY_HEADER = "X-Clear-Record-Delivery"

#: ``CR_WEBHOOKS``: a JSON array of endpoint objects, the ``CR_*`` layer of the
#: ADR-0007 precedence. It beats the config file but loses to an explicit
#: argument.
ENV_ENDPOINTS = "CR_WEBHOOKS"

#: How many delivery outcomes to keep for health inspection.
DELIVERY_HISTORY = 200


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def sign(secret: str, body: bytes) -> str:
    """The HMAC-SHA256 hex digest of ``body`` under ``secret``.

    The receiver recomputes this over the exact request body and compares it to
    the signature header (``sha256=<digest>``) with a constant-time compare.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@dataclasses.dataclass(frozen=True)
class WebhookEndpoint:
    """One user-configured receiver.

    ``events`` filters the events sent here; empty means every event. Secret
    material is **never** stored: ``secret_env`` names the environment variable
    the secret is read from at delivery time.
    """

    url: str
    events: tuple[str, ...] = ()
    include_content: bool = False
    secret_env: str | None = None
    name: str | None = None

    def wants(self, event_type: str) -> bool:
        """Whether this endpoint subscribed to ``event_type`` (empty = all)."""
        return not self.events or event_type in self.events


@dataclasses.dataclass(frozen=True)
class WebhookEvent:
    """One event to deliver: its identity and the ids it concerns."""

    id: str
    type: str
    occurred_at: str
    project_id: int | None = None
    meeting_id: int | None = None
    run_id: int | None = None
    content: dict | None = None

    def payload(self, *, include_content: bool) -> dict:
        """The JSON payload for one endpoint.

        Content is attached only when the endpoint opted in *and* the producer
        supplied it; otherwise the payload is metadata only.
        """
        data: dict = {
            "event": self.id,
            "type": self.type,
            "occurred_at": self.occurred_at,
        }
        if self.project_id is not None:
            data["project_id"] = self.project_id
        if self.meeting_id is not None:
            data["meeting_id"] = self.meeting_id
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if include_content and self.content is not None:
            data["content"] = self.content
        return data

    def body(self, *, include_content: bool) -> bytes:
        """The exact bytes posted and signed for this endpoint."""
        return json.dumps(
            self.payload(include_content=include_content),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class Delivery:
    """The recorded outcome of delivering one event to one endpoint."""

    event_id: str
    type: str
    url: str
    #: ``"delivered"`` or ``"failed"``.
    status: str
    attempts: int
    http_status: int | None
    error: str | None
    at: str


@dataclasses.dataclass(frozen=True)
class EndpointReport:
    """One endpoint's config health and its most recent delivery, as a reader sees it.

    A read-only *view*: ``problems`` is this endpoint's slice of the existing
    config-problem surface — the rule that raised a problem lives where it was
    raised, and nothing here re-decides what makes a config valid — and
    ``last_delivery`` is the newest entry for this URL in the bounded delivery
    history. It carries no secret material: the signing secret is read from the
    environment at delivery time and never stored (ADR-0020), so only the fact
    that the endpoint is signed (``signed``) is exposed.
    """

    name: str | None
    url: str
    events: tuple[str, ...]
    include_content: bool
    #: Whether the endpoint names a signing secret; the secret itself is never held.
    signed: bool
    problems: tuple[str, ...]
    last_delivery: Delivery | None

    @property
    def health(self) -> str:
        """One of ``config_problem`` / ``delivery_failed`` / ``delivered`` / ``no_delivery_yet``."""
        if self.problems:
            return "config_problem"
        if self.last_delivery is None:
            return "no_delivery_yet"
        return (
            "delivered"
            if self.last_delivery.status == "delivered"
            else "delivery_failed"
        )


@dataclasses.dataclass(frozen=True)
class WebhookStatus:
    """The console's whole view: the config problems and one report per endpoint.

    :attr:`state` keeps apart the three silences that must not be confused
    (ADR-0020's review catch — a silently disabled webhook is indistinguishable
    from "no events happened"):

    - ``"not_configured"`` — no endpoint is set up, so nothing is delivered.
      This is **not** "healthy" and **not** "failing"; it is opt-out.
    - ``"config_problem"`` — resolving the config found a problem; delivery is
      off entirely or affected for one endpoint. This is the state a malformed
      config used to hide in.
    - ``"delivery_failed"`` / ``"no_delivery_yet"`` / ``"ok"`` — the config is
      valid, so the delivery history decides: a failure, nothing delivered yet,
      or a success.

    ``problems`` is :attr:`WebhookEmitter.problems` verbatim — the same surface
    that already warns on stderr, never a recomputation.
    """

    endpoints: tuple[EndpointReport, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        """``not_configured`` / ``config_problem`` / ``delivery_failed`` / ``no_delivery_yet`` / ``ok``."""
        if not self.endpoints and not self.problems:
            return "not_configured"
        if self.problems:
            return "config_problem"
        if any(report.health == "delivery_failed" for report in self.endpoints):
            return "delivery_failed"
        if self.endpoints and all(
            report.last_delivery is None for report in self.endpoints
        ):
            # A configured endpoint that has never sent is its own silence;
            # "ok" would claim a success that has not happened.
            return "no_delivery_yet"
        return "ok"


# --- configuration (ADR-0007 precedence) ----------------------------------- #

#: The TOML schema, documented where the user sets it::
#:
#:     [[webhooks.endpoints]]
#:     url = "https://example.test/hooks/clear-record"
#:     events = ["run.finished", "archive.created"]  # omit for all events
#:     include_content = false                        # private: default off
#:     secret_env = "CR_WEBHOOK_SECRET"               # never the value itself


@dataclasses.dataclass(frozen=True)
class WebhookConfig:
    """The resolved endpoints and any problems found while resolving them.

    ``problems`` is what makes a misconfiguration visible: a user asking "why is
    nothing being delivered?" can read it — and the matching stderr warning —
    instead of guessing. Resolving never raises; a bad entry is reported and
    skipped, so the app keeps running.
    """

    endpoints: tuple[WebhookEndpoint, ...] = ()
    problems: tuple[str, ...] = ()
    #: Per-endpoint slices of ``problems``, parallel to ``endpoints``: which of
    #: the problems concern each *configured* endpoint. A projection of the same
    #: detection that built ``problems`` — never a second opinion about validity.
    #: A problem that names no configured endpoint (a skipped or malformed entry)
    #: stays in ``problems`` only.
    endpoint_problems: tuple[tuple[str, ...], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the config resolved with no problems."""
        return not self.problems


def _warn(problem: str) -> None:
    """Surface a config problem on stderr, as one line — never a traceback."""
    print(f"clear-record: webhook config: {problem}", file=sys.stderr)


def _parse_endpoint(
    raw: Mapping, *, source: str
) -> tuple[WebhookEndpoint | None, str | None]:
    """One endpoint from a config table or env JSON object: ``(endpoint, problem)``."""
    if not isinstance(raw, Mapping):
        return None, f"{source}: each endpoint must be a table/object"
    url = str(raw.get("url") or "").strip()
    if not url:
        return None, f"{source}: endpoint is missing 'url'"
    raw_events = raw.get("events")
    if raw_events is None:
        events: tuple[str, ...] = ()
    elif isinstance(raw_events, str):
        events = tuple(part.strip() for part in raw_events.split(",") if part.strip())
    else:
        events = tuple(str(part).strip() for part in raw_events if str(part).strip())
    unknown = sorted({name for name in events if name not in ALL_EVENTS})
    if unknown:
        return None, f"{source}: unknown event(s) {unknown}; choose from {ALL_EVENTS}"
    secret_env = raw.get("secret_env")
    name = raw.get("name")
    return (
        WebhookEndpoint(
            url=url,
            events=events,
            include_content=bool(raw.get("include_content", False)),
            secret_env=str(secret_env).strip() if secret_env else None,
            name=str(name).strip() if name else None,
        ),
        None,
    )


def _coerce_endpoints(
    endpoints: Iterable, *, source: str
) -> tuple[tuple[WebhookEndpoint, ...], tuple[str, ...]]:
    """Parse a sequence of endpoint entries into ``(endpoints, problems)``."""
    out: list[WebhookEndpoint] = []
    problems: list[str] = []
    for index, raw in enumerate(endpoints):
        if isinstance(raw, WebhookEndpoint):
            out.append(raw)
            continue
        endpoint, problem = _parse_endpoint(raw, source=f"{source}[{index}]")
        if problem is not None:
            problems.append(problem)
        elif endpoint is not None:
            out.append(endpoint)
    return tuple(out), tuple(problems)


def _config_endpoints(
    config_file: str | Path | None = None,
) -> tuple[tuple[WebhookEndpoint, ...], tuple[str, ...]]:
    """Endpoints from the ``[webhooks]`` table of the config file.

    A missing config is **not** a problem (webhooks are opt-in). A config that is
    present but unreadable or malformed **is**: it becomes a problem rather than
    being silently ignored.
    """
    path = Path(config_file) if config_file is not None else config_path()
    if not path.is_file():
        return (), ()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (), (f"{path}: not valid TOML ({exc})",)
    webhooks = data.get("webhooks")
    if webhooks is None:
        return (), ()
    if not isinstance(webhooks, dict):
        return (), (f"{path}: [webhooks] must be a table",)
    raw = webhooks.get("endpoints") or []
    if not isinstance(raw, list):
        return (), (f"{path}: webhooks.endpoints must be an array of tables",)
    return _coerce_endpoints(raw, source=str(path))


def _secret_problems(
    endpoints: Sequence[WebhookEndpoint], environ: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Flag endpoints that named a secret env the environment does not set.

    Returns the flat problems *and* the same problems grouped per endpoint
    (parallel to ``endpoints``). The detection rule lives here, once: the flat
    tuple is what :attr:`WebhookConfig.problems` has always been, and the grouped
    view is what lets the console say *which* endpoint is broken without
    restating the rule.
    """
    flat: list[str] = []
    grouped: list[tuple[str, ...]] = []
    for endpoint in endpoints:
        if endpoint.secret_env and not environ.get(endpoint.secret_env):
            problem = (
                f"endpoint {endpoint.url!r} names secret_env "
                f"{endpoint.secret_env!r}, which is not set: it will fail closed "
                "and deliver nothing"
            )
            flat.append(problem)
            grouped.append((problem,))
        else:
            grouped.append(())
    return tuple(flat), tuple(grouped)


def load_webhook_config(
    endpoints: Sequence[WebhookEndpoint | Mapping] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
) -> WebhookConfig:
    """Resolve the configured endpoints and collect any problems; never raises.

    Precedence (ADR-0007): explicit argument > ``CR_WEBHOOKS`` > config file >
    none. Every problem found is written to stderr as a one-line warning and
    returned in :attr:`WebhookConfig.problems`, so a misconfiguration is visible
    and inspectable instead of silently meaning "no notifications".
    """
    env = os.environ if environ is None else environ
    if endpoints is not None:
        resolved, problems = _coerce_endpoints(endpoints, source="explicit endpoints")
    else:
        raw_env = env.get(ENV_ENDPOINTS)
        if raw_env and raw_env.strip():
            try:
                parsed = json.loads(raw_env)
            except json.JSONDecodeError as exc:
                resolved, problems = (), (f"${ENV_ENDPOINTS} is not valid JSON: {exc}",)
            else:
                if not isinstance(parsed, list):
                    resolved, problems = (
                        (),
                        (f"${ENV_ENDPOINTS} must be a JSON array of objects",),
                    )
                else:
                    resolved, problems = _coerce_endpoints(
                        parsed, source=f"${ENV_ENDPOINTS}"
                    )
        else:
            resolved, problems = _config_endpoints(config_file)
    secret_flat, endpoint_problems = _secret_problems(resolved, env)
    problems = problems + secret_flat
    for problem in problems:
        _warn(problem)
    return WebhookConfig(
        endpoints=resolved,
        problems=problems,
        endpoint_problems=endpoint_problems,
    )


def resolve_endpoints(
    endpoints: Sequence[WebhookEndpoint | Mapping] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
) -> tuple[WebhookEndpoint, ...]:
    """The resolved endpoints only; see :func:`load_webhook_config` for problems."""
    config = load_webhook_config(endpoints, environ=environ, config_file=config_file)
    return config.endpoints


# --- the emitter ------------------------------------------------------------ #


class WebhookEmitter:
    """Delivers events to the configured endpoints on a background worker.

    ``emit`` only enqueues, so it is safe to call from a pipeline run's thread
    and can never slow a run down. The worker performs the POSTs, retries a
    bounded number of times with exponential backoff, and records each
    delivery's outcome. With no endpoints the emitter is inert: no worker
    thread is created and ``emit`` is a no-op.

    ``problems`` carries any config problems found while resolving the endpoints
    (see :func:`load_webhook_config`); :meth:`from_config` fills it in, so a bad
    config is inspectable on the emitter the app is actually using.
    :meth:`status` packages that surface with the last delivery per endpoint for
    a console; it changes nothing about how delivery runs.
    """

    def __init__(
        self,
        endpoints: Sequence[WebhookEndpoint] = (),
        *,
        problems: Sequence[str] = (),
        endpoint_problems: Sequence[Sequence[str]] = (),
        timeout: float = 5.0,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        opener=None,
        sleep=time.sleep,
        clock=_now,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._endpoints = tuple(endpoints)
        self._problems = tuple(problems)
        self._endpoint_problems = tuple(tuple(entry) for entry in endpoint_problems)
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._clock = clock
        self._condition = threading.Condition()
        self._queue: queue.Queue[tuple[WebhookEvent, WebhookEndpoint] | None] = (
            queue.Queue()
        )
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._deliveries: deque[Delivery] = deque(maxlen=DELIVERY_HISTORY)

    @classmethod
    def from_config(
        cls,
        endpoints: Sequence[WebhookEndpoint | Mapping] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        config_file: str | Path | None = None,
        **kwargs,
    ) -> WebhookEmitter:
        """Build an emitter from the resolved config (see :func:`load_webhook_config`).

        Any config problem is attached to the emitter's :attr:`problems` and a
        warning is written to stderr; construction never raises.
        """
        config = load_webhook_config(
            endpoints, environ=environ, config_file=config_file
        )
        return cls(
            config.endpoints,
            problems=config.problems,
            endpoint_problems=config.endpoint_problems,
            **kwargs,
        )

    @property
    def endpoints(self) -> tuple[WebhookEndpoint, ...]:
        return self._endpoints

    @property
    def problems(self) -> tuple[str, ...]:
        """The config problems found while resolving this emitter's endpoints."""
        return self._problems

    @property
    def enabled(self) -> bool:
        """Whether any endpoint is configured."""
        return bool(self._endpoints)

    def emit(
        self,
        event_type: str,
        *,
        project_id: int | None = None,
        meeting_id: int | None = None,
        run_id: int | None = None,
        content: dict | None = None,
        occurred_at: str | None = None,
    ) -> WebhookEvent | None:
        """Enqueue ``event_type`` for every subscribed endpoint; never raises.

        Returns the event (with its id) when at least one endpoint subscribed,
        else ``None``. The POSTs happen on the worker thread.
        """
        try:
            matching = tuple(e for e in self._endpoints if e.wants(event_type))
            if not matching:
                return None
            event = WebhookEvent(
                id=str(uuid.uuid4()),
                type=event_type,
                occurred_at=occurred_at or self._clock(),
                project_id=project_id,
                meeting_id=meeting_id,
                run_id=run_id,
                content=content,
            )
            with self._condition:
                if self._closed:
                    return None
                self._ensure_worker()
                for endpoint in matching:
                    self._queue.put((event, endpoint))
                    self._pending += 1
                self._idle.clear()
            return event
        except Exception:  # noqa: BLE001 - delivery must never affect the caller
            return None

    def deliveries(self) -> tuple[Delivery, ...]:
        """The recorded delivery outcomes, oldest first."""
        with self._condition:
            return tuple(self._deliveries)

    def status(self) -> WebhookStatus:
        """The console's read-only view: config validity and last delivery per endpoint.

        Reads only state the emitter already keeps — :attr:`problems` (verbatim,
        sliced per endpoint) and the bounded delivery history — so it never
        recomputes a config rule and never touches the network. It is safe to
        call from a request thread: it takes the emitter's lock and copies; an
        unconfigured emitter reports ``state == "not_configured"``, which is
        kept distinct from both "healthy" and "failing" (ADR-0020).
        """
        with self._condition:
            deliveries = tuple(self._deliveries)
        reports: list[EndpointReport] = []
        for index, endpoint in enumerate(self._endpoints):
            own = (
                self._endpoint_problems[index]
                if index < len(self._endpoint_problems)
                else ()
            )
            last = next(
                (d for d in reversed(deliveries) if d.url == endpoint.url), None
            )
            reports.append(
                EndpointReport(
                    name=endpoint.name,
                    url=endpoint.url,
                    events=endpoint.events,
                    include_content=endpoint.include_content,
                    signed=endpoint.secret_env is not None,
                    problems=own,
                    last_delivery=last,
                )
            )
        return WebhookStatus(endpoints=tuple(reports), problems=self._problems)

    def flush(self, timeout: float | None = None) -> bool:
        """Block until every queued delivery has finished (tests). Returns done."""
        return self._idle.wait(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Stop the worker once its queue is drained (tests, shutdown)."""
        with self._condition:
            self._closed = True
            thread = self._thread
            self._queue.put(None)
        if thread is not None:
            thread.join(timeout)

    # --- worker ------------------------------------------------------------- #
    def _ensure_worker(self) -> None:
        if self._thread is None and not self._closed:
            self._thread = threading.Thread(
                target=self._work, name="cr-webhooks", daemon=True
            )
            self._thread.start()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # sentinel: shutdown requested
                self._queue.task_done()
                return
            try:
                self._deliver(*job)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                event, endpoint = job
                self._record(
                    Delivery(
                        event_id=event.id,
                        type=event.type,
                        url=endpoint.url,
                        status="failed",
                        attempts=0,
                        http_status=None,
                        error=f"{type(exc).__name__}: {exc}",
                        at=self._clock(),
                    )
                )
            finally:
                self._queue.task_done()
                with self._condition:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._idle.set()
                    self._condition.notify_all()

    def _deliver(self, event: WebhookEvent, endpoint: WebhookEndpoint) -> Delivery:
        def _outcome(
            status: str, attempts: int, http_status: int | None, error: str | None
        ) -> Delivery:
            """Record one delivery outcome for this event and endpoint."""
            return self._record(
                Delivery(
                    event_id=event.id,
                    type=event.type,
                    url=endpoint.url,
                    status=status,
                    attempts=attempts,
                    http_status=http_status,
                    error=error,
                    at=self._clock(),
                )
            )

        body = event.body(include_content=endpoint.include_content)
        secret = os.environ.get(endpoint.secret_env) if endpoint.secret_env else None
        if endpoint.secret_env and not secret:
            # Fail closed: an endpoint that asked for a signature must never
            # silently receive an unsigned request. Surface the dropped event,
            # not just the recorded outcome, so the reason is visible.
            problem = (
                f"endpoint {endpoint.url!r} names secret_env {endpoint.secret_env!r}, "
                "which is not set: refusing to send unsigned"
            )
            _warn(problem)
            return _outcome("failed", 0, None, problem)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "clear-record-webhooks/1",
            EVENT_HEADER: event.type,
            DELIVERY_HEADER: event.id,
        }
        if secret:
            headers[SIGNATURE_HEADER] = f"sha256={sign(secret, body)}"

        http_status: int | None = None
        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                request = urllib.request.Request(
                    endpoint.url, data=body, headers=headers, method="POST"
                )
                response = self._opener(request, timeout=self._timeout)
                try:
                    status = getattr(response, "status", None)
                    if status is None:
                        status = response.getcode()
                    http_status = int(status)
                finally:
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                if 200 <= http_status < 300:
                    return _outcome("delivered", attempt, http_status, None)
                last_error = f"HTTP {http_status}"
            except urllib.error.HTTPError as exc:
                http_status = exc.code
                last_error = f"HTTP {exc.code}"
                exc.close()
            except Exception as exc:  # noqa: BLE001 - recorded, then retried
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self._max_attempts:
                self._sleep(self._backoff_base * (2 ** (attempt - 1)))
        return _outcome("failed", self._max_attempts, http_status, last_error)

    def _record(self, delivery: Delivery) -> Delivery:
        with self._condition:
            self._deliveries.append(delivery)
        return delivery


# --- process-wide default --------------------------------------------------- #

_DEFAULT_LOCK = threading.Lock()
_DEFAULT: WebhookEmitter | None = None


def default_emitter() -> WebhookEmitter:
    """The shared config-driven emitter (one worker for the process).

    A malformed webhook config never raises; it is surfaced as a stderr warning
    and recorded on the emitter's :attr:`WebhookEmitter.problems` — the visible
    answer to "why is nothing being delivered?". The resolved config is cached
    for the process lifetime.
    """
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                try:
                    _DEFAULT = WebhookEmitter.from_config()
                except Exception as exc:  # noqa: BLE001 - never take the app down
                    problem = f"webhooks disabled: {type(exc).__name__}: {exc}"
                    _warn(problem)
                    _DEFAULT = WebhookEmitter((), problems=(problem,))
    return _DEFAULT


def reset_default_emitter() -> None:
    """Drop the cached default emitter (tests that reconfigure the process)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


__all__ = [
    "ALL_EVENTS",
    "ARCHIVE_CREATED",
    "DELIVERY_HEADER",
    "DELIVERY_HISTORY",
    "Delivery",
    "EMITTED_EVENTS",
    "EndpointReport",
    "ENV_ENDPOINTS",
    "EVENT_HEADER",
    "FUTURE_EVENTS",
    "SIGNATURE_HEADER",
    "TRANSCRIPT_READY",
    "WebhookConfig",
    "WebhookEmitter",
    "WebhookEndpoint",
    "WebhookEvent",
    "WebhookStatus",
    "default_emitter",
    "load_webhook_config",
    "reset_default_emitter",
    "resolve_endpoints",
    "sign",
]
