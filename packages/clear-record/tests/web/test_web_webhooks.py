"""The console's webhook status surface (ADR-0020).

Three silences must never be confused: **not configured** (nothing set up),
**config broken** (a malformed config that used to disable delivery silently)
and **delivery failing** (the receiver rejected or was unreachable). These tests
drive the surface through the real app with an injected emitter, so no config
file and no network are involved.

The secret rule is asserted directly: the signing secret is read from the
environment at delivery time, never held by the emitter, and must not appear in
the JSON API, the HTML panel or the page source — not masked, not truncated.
"""

from __future__ import annotations

import json

from _console import signed_in
from clear_record.service import Registry, WebhookEndpoint
from clear_record.service.webhooks import DELIVERY_HISTORY, WebhookEmitter
from clear_record.web.app import create_app
from fastapi.testclient import TestClient


class _Response:
    """The slice of an ``http.client`` response the emitter reads."""

    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:  # pragma: no cover - the emitter reads ``status``
        return self.status

    def close(self) -> None:
        pass


def _ok(status: int = 200):
    def opener(request, timeout=None) -> _Response:
        return _Response(status)

    return opener


def _refused(message: str = "connection refused"):
    def opener(request, timeout=None):
        raise OSError(message)

    return opener


def _client(tmp_path, emitter: WebhookEmitter) -> TestClient:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    return signed_in(TestClient(create_app(registry, webhooks=emitter)))


def _deliver(emitter: WebhookEmitter, event_type: str = "run.finished") -> None:
    assert emitter.emit(event_type, project_id=1, meeting_id=2, run_id=3) is not None
    assert emitter.flush(timeout=5)


# --- the three silences ------------------------------------------------------ #


def test_not_configured_is_neither_healthy_nor_failing(tmp_path) -> None:
    """An opt-out must read as "not configured", not as success or failure."""
    client = _client(tmp_path, WebhookEmitter(()))

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "not_configured"
    assert view["configured"] is False
    assert view["problems"] == []
    assert view["endpoints"] == []

    panel = client.get("/web/ui/webhooks").text
    assert "Not configured" in panel
    assert "No webhook endpoints are configured" in panel
    # The opt-out is not dressed as either of the two states it must stay apart
    # from: no "Delivered" badge and no failure badge.
    assert "Delivered" not in panel
    assert "Failing" not in panel


def test_a_malformed_config_is_a_problem_not_silence(tmp_path) -> None:
    """The ADR-0020 review catch: a bad config must not look like "no events"."""
    emitter = WebhookEmitter.from_config(
        [{"url": "http://x.test/hook", "events": ["run.finishd"]}], environ={}
    )
    assert emitter.problems  # the service already found it
    client = _client(tmp_path, emitter)

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "config_problem"
    # The config surface is the emitter's own, verbatim — never recomputed here.
    assert view["problems"] == list(emitter.problems)
    assert view["endpoints"] == []
    assert "unknown event" in client.get("/web/ui/webhooks").text


def test_a_named_secret_that_is_unset_is_per_endpoint(tmp_path) -> None:
    """One broken endpoint is marked; a healthy sibling is not tarred with it."""
    emitter = WebhookEmitter.from_config(
        [
            {"url": "http://good.test/hook"},
            {"url": "http://bad.test/hook", "secret_env": "CR_MISSING_SECRET"},
        ],
        environ={},
    )
    client = _client(tmp_path, emitter)

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "config_problem"
    good, bad = view["endpoints"]
    assert good["health"] == "no_delivery_yet"
    assert good["problems"] == []
    assert bad["health"] == "config_problem"
    assert bad["problems"]
    assert "CR_MISSING_SECRET" in bad["problems"][0]


def test_a_configured_endpoint_with_no_delivery_is_not_labelled_ok(tmp_path) -> None:
    """A configured endpoint that has never sent reads "No delivery yet"."""
    emitter = WebhookEmitter([WebhookEndpoint(url="http://never.test/hook")])
    try:
        client = _client(tmp_path, emitter)

        view = client.get("/api/v1/webhooks").json()

        assert view["state"] == "no_delivery_yet"
        # The aggregate label must not fall through to the neutral "Configured"
        # (that read as a success the endpoint has not had).
        assert view["state_label"] == "No delivery yet"
        assert "No delivery yet" in client.get("/web/ui/webhooks").text
    finally:
        emitter.close(timeout=5)


# --- the last delivery outcome ---------------------------------------------- #


def test_a_delivered_event_is_recorded_and_shown(tmp_path) -> None:
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://receiver.test/hook", name="Ops hook")],
        opener=_ok(204),
        backoff_base=0.0,
    )
    try:
        _deliver(emitter)
    finally:
        emitter.close(timeout=5)
    client = _client(tmp_path, emitter)

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "ok"
    endpoint = view["endpoints"][0]
    assert endpoint["name"] == "Ops hook"
    assert endpoint["health"] == "delivered"
    last = endpoint["last_delivery"]
    assert last["outcome"] == "delivered"
    assert last["http_status"] == 204
    assert last["attempts"] == 1
    assert last["error"] is None
    assert last["at"]


def test_a_failed_delivery_shows_the_http_status_and_err(tmp_path) -> None:
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://receiver.test/hook")],
        opener=_ok(500),
        max_attempts=1,
        backoff_base=0.0,
    )
    try:
        _deliver(emitter)
    finally:
        emitter.close(timeout=5)
    client = _client(tmp_path, emitter)

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "delivery_failed"
    endpoint = view["endpoints"][0]
    assert endpoint["health"] == "delivery_failed"
    assert endpoint["last_delivery"]["http_status"] == 500
    assert "500" in endpoint["last_delivery"]["error"]
    # The reason is legible on the panel too, so a user can tell a rejected
    # signature from an unreachable host.
    assert "500" in client.get("/web/ui/webhooks").text


def test_an_unreachable_host_is_distinguishable_from_a_bad_status(tmp_path) -> None:
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://receiver.test/hook")],
        opener=_refused(),
        max_attempts=1,
        backoff_base=0.0,
    )
    try:
        _deliver(emitter)
    finally:
        emitter.close(timeout=5)
    client = _client(tmp_path, emitter)

    last = client.get("/api/v1/webhooks").json()["endpoints"][0]["last_delivery"]

    assert last["outcome"] == "failed"
    assert last["http_status"] is None  # no response at all
    assert "connection refused" in last["error"]


# --- the secret stays secret ------------------------------------------------- #


def test_the_signing_secret_never_reaches_the_console(tmp_path, monkeypatch) -> None:
    """The value is not in the JSON, the panel or the page source — only *that*
    the endpoint is signed.

    The secret's environment variable is named with a sentinel value; the config
    is valid (the variable is set), so the emitter never even records the name.
    """
    secret = "s3cret-value-that-must-not-appear"
    monkeypatch.setenv("CR_HOOK_SECRET", secret)
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://receiver.test/hook", secret_env="CR_HOOK_SECRET")],
        opener=_ok(200),
        backoff_base=0.0,
    )
    try:
        _deliver(emitter)
        assert emitter.deliveries()[0].status == "delivered"
    finally:
        emitter.close(timeout=5)
    client = _client(tmp_path, emitter)

    api = client.get("/api/v1/webhooks")
    panel = client.get("/web/ui/webhooks")
    home = client.get("/web/")

    for response in (api, panel, home):
        assert secret not in response.text

    view = api.json()
    endpoint = view["endpoints"][0]
    assert endpoint["signed"] is True  # the fact is shown...
    assert "CR_HOOK_SECRET" not in json.dumps(view)  # ...the variable name is not
    assert "secret_env" not in json.dumps(view)


def test_a_config_problem_names_the_variable_not_the_value(tmp_path) -> None:
    """Even the loud "unset secret" path carries no secret material."""
    emitter = WebhookEmitter.from_config(
        [{"url": "http://receiver.test/hook", "secret_env": "CR_UNSET_SECRET"}],
        environ={},
    )
    client = _client(tmp_path, emitter)

    view = client.get("/api/v1/webhooks").json()

    assert view["state"] == "config_problem"
    # The problem names the *variable* to set (the config field is ``secret_env``),
    # never a value; there is no value to leak for an unset variable.
    assert "CR_UNSET_SECRET" in view["problems"][0]


# --- the state is bounded ---------------------------------------------------- #


def test_the_delivery_history_is_a_bounded_ring(tmp_path) -> None:
    """Delivery state never grows without limit (a small ring, not a log)."""
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://receiver.test/hook")],
        opener=_ok(200),
        backoff_base=0.0,
    )
    try:
        for _ in range(DELIVERY_HISTORY + 5):
            _deliver(emitter)
        deliveries = emitter.deliveries()
        status = emitter.status()
    finally:
        emitter.close(timeout=5)

    assert len(deliveries) == DELIVERY_HISTORY
    assert status.endpoints[0].last_delivery is deliveries[-1]
    # The surface is bounded with the ring it reads.
    assert len(status.endpoints) == 1
