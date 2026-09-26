"""Outbound webhooks: delivery, retries, signing, config and run isolation.

Every delivery test runs against a **local fake receiver** — a stdlib
:mod:`http.server` on an ephemeral port — so no network is touched and the
request bytes (and signature) can be asserted exactly. A delivery that fails or
times out must never change what a run records, which is asserted directly.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from clear_record.service import (
    Registry,
    RunManager,
    WebhookEmitter,
    WebhookEndpoint,
    archive_meeting,
)
from clear_record.service import webhooks as webhooks_module
from clear_record.service.webhooks import (
    SIGNATURE_HEADER,
    load_webhook_config,
    resolve_endpoints,
    sign,
)


# --- a local fake receiver -------------------------------------------------- #


class _SilentServer(http.server.ThreadingHTTPServer):
    """A server that does not print a traceback when a client times out."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        pass


class _Receiver:
    """A stdlib HTTP server that records the POSTs it receives."""

    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.status = status
        self.delay = delay
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                with receiver._lock:
                    receiver.requests.append((dict(self.headers.items()), body))
                if receiver.delay:
                    time.sleep(receiver.delay)
                self.send_response(receiver.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:  # noqa: D102
                pass

        self._server = _SilentServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/hook"

    def __enter__(self) -> _Receiver:
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup over the receiver's captured headers."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# --- registry / run helpers ------------------------------------------------- #


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(registry: Registry, tmp_path: Path, tapes: list[Path]):
    registry.create_project(
        "Ops",
        actor="console",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor="console",
    )
    registry.set_recording_set(
        meeting.id,
        [str(tape) for tape in tapes],
        actor="console",
    )
    return meeting


def _seeded_archive(tmp_path):
    registry = _registry(tmp_path)
    root = tmp_path / "archive"
    registry.create_project(
        "Ops",
        default_archive_root=str(root),
        actor="console",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake-audio")
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor="console",
    )
    registry.set_recording_set(
        meeting.id,
        [str(tape)],
        actor="console",
    )
    record = workspace / "record.json"
    record.write_bytes(b'{"segments": []}')
    registry.add_artifact(
        meeting.id,
        kind="record",
        path=str(record),
        actor="console",
    )
    return registry, meeting


# --- delivery: success, signature, content-free ----------------------------- #


def test_successful_delivery_is_signed_and_content_free(monkeypatch) -> None:
    with _Receiver() as receiver:
        monkeypatch.setenv("CR_TEST_HOOK_SECRET", "s3cret")
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url, secret_env="CR_TEST_HOOK_SECRET")],
            backoff_base=0.001,
        )
        try:
            event = emitter.emit("run.finished", project_id=7, meeting_id=8, run_id=9)
            assert event is not None
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    assert len(receiver.requests) == 1
    headers, body = receiver.requests[0]
    payload = json.loads(body)
    assert payload["event"] == event.id
    assert payload["type"] == "run.finished"
    assert payload["occurred_at"]
    assert payload["project_id"] == 7
    assert payload["meeting_id"] == 8
    assert payload["run_id"] == 9
    assert "content" not in payload  # content-free by default

    # The receiver can verify the HMAC over the exact bytes it received.
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    sent = _header(headers, SIGNATURE_HEADER)
    assert sent == f"sha256={expected}"
    assert sign("s3cret", body) == expected
    assert hmac.compare_digest(sent.removeprefix("sha256="), expected)

    delivery = emitter.deliveries()[0]
    assert delivery.status == "delivered"
    assert delivery.attempts == 1
    assert delivery.http_status == 200
    assert delivery.error is None


def test_a_named_secret_that_is_unset_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("CR_MISSING_SECRET", raising=False)
    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url, secret_env="CR_MISSING_SECRET")],
            backoff_base=0.001,
        )
        try:
            emitter.emit("run.finished", project_id=1)
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)
    assert receiver.requests == []  # no silent unsigned downgrade
    delivery = emitter.deliveries()[0]
    assert delivery.status == "failed"
    assert "refusing to send unsigned" in (delivery.error or "")


def test_content_is_stripped_unless_the_endpoint_opts_in() -> None:
    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [
                WebhookEndpoint(url=receiver.url),  # metadata only
                WebhookEndpoint(url=receiver.url, include_content=True),  # opted in
            ],
            backoff_base=0.001,
        )
        try:
            emitter.emit(
                "transcript.ready",
                project_id=1,
                meeting_id=2,
                run_id=3,
                content={"summary": "private words"},
            )
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    first = json.loads(receiver.requests[0][1])
    second = json.loads(receiver.requests[1][1])
    assert "content" not in first
    assert second["content"] == {"summary": "private words"}


# --- delivery: retries, non-2xx, timeout ------------------------------------ #


def test_non_2xx_is_retried_within_the_bound_and_recorded_failed() -> None:
    with _Receiver(status=500) as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], max_attempts=3, backoff_base=0.0
        )
        try:
            emitter.emit("run.started", project_id=1, meeting_id=2, run_id=3)
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    assert len(receiver.requests) == 3  # bounded, not infinite
    delivery = emitter.deliveries()[0]
    assert delivery.status == "failed"
    assert delivery.attempts == 3
    assert delivery.http_status == 500
    assert "500" in (delivery.error or "")


def test_max_attempts_is_a_configurable_bound() -> None:
    with _Receiver(status=503) as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], max_attempts=1, backoff_base=0.0
        )
        try:
            emitter.emit("run.started")
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)
    assert len(receiver.requests) == 1
    assert emitter.deliveries()[0].attempts == 1


def test_a_timeout_neither_blocks_emit_nor_hangs_the_worker() -> None:
    with _Receiver(delay=0.5) as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)],
            timeout=0.1,
            max_attempts=1,
            backoff_base=0.0,
        )
        try:
            start = time.monotonic()
            event = emitter.emit("archive.created", project_id=1, meeting_id=2)
            emit_elapsed = time.monotonic() - start
            assert event is not None
            # emit only enqueues; it does not wait for the slow receiver.
            assert emit_elapsed < 0.2
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    delivery = emitter.deliveries()[0]
    assert delivery.status == "failed"
    assert delivery.http_status is None
    assert delivery.error  # a timeout was recorded, not swallowed


def test_an_unreachable_endpoint_is_recorded_and_never_raises() -> None:
    # Port 1 on the loopback interface refuses instantly.
    emitter = WebhookEmitter(
        [WebhookEndpoint(url="http://127.0.0.1:1/hook")],
        max_attempts=2,
        backoff_base=0.0,
        timeout=0.2,
    )
    try:
        assert emitter.emit("run.failed", project_id=1, meeting_id=2, run_id=3)
        assert emitter.flush(timeout=5)
    finally:
        emitter.close(timeout=5)
    delivery = emitter.deliveries()[0]
    assert delivery.status == "failed"
    assert delivery.attempts == 2


# --- event filtering and inertness ------------------------------------------ #


def test_an_endpoint_filters_events_and_an_empty_emitter_is_inert() -> None:
    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url, events=("run.failed",))],
            backoff_base=0.001,
        )
        try:
            assert emitter.emit("run.finished") is None  # not subscribed
            assert emitter.emit("run.failed", run_id=4) is not None
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)
    assert len(receiver.requests) == 1
    assert json.loads(receiver.requests[0][1])["type"] == "run.failed"

    inert = WebhookEmitter(())
    assert inert.enabled is False
    assert inert.emit("run.started") is None
    inert.close()


def test_status_is_not_ok_before_any_delivery() -> None:
    """A configured endpoint that has never sent is its own state, not 'ok'."""
    emitter = WebhookEmitter([WebhookEndpoint(url="http://127.0.0.1:1/hook")])
    try:
        status = emitter.status()
    finally:
        emitter.close(timeout=5)
    assert status.endpoints[0].health == "no_delivery_yet"
    assert status.state == "no_delivery_yet"


# --- run events ------------------------------------------------------------- #


def test_a_successful_run_emits_started_finished_and_transcript_ready(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def pipeline(directory, options, on_event) -> None:
        (Path(directory) / "record.json").write_text("{}", encoding="utf-8")
        (Path(directory) / "segments.json").write_text("{}", encoding="utf-8")

    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], backoff_base=0.001
        )
        try:
            manager = RunManager(registry, pipeline=pipeline, webhooks=emitter)
            state = manager.wait(
                manager.start(meeting, origin="console", actor="console").id, timeout=10
            )
            assert state.status == "done"
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    types = [json.loads(body)["type"] for _headers, body in receiver.requests]
    assert types == ["run.started", "run.finished", "transcript.ready"]


def _wait_for_events(
    receiver: _Receiver, expected: list[str], *, timeout: float = 5.0
) -> list[str]:
    """Poll the receiver until every expected event type has arrived.

    ``emitter.flush()`` only reports that the queue is empty at that instant; a
    run's terminal event is emitted by the run thread after the terminal state is
    recorded, so it can be enqueued just after the flush returns. Waiting on the
    real clock is what makes this deterministic.
    """
    deadline = time.monotonic() + timeout
    while True:
        with receiver._lock:
            types = [json.loads(body)["type"] for _headers, body in receiver.requests]
        if all(name in types for name in expected) or time.monotonic() >= deadline:
            return types
        time.sleep(0.005)


def test_a_failed_run_emits_run_failed(tmp_path) -> None:
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def boom(directory, options, on_event) -> None:
        raise RuntimeError("backend unavailable")

    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], backoff_base=0.001
        )
        try:
            manager = RunManager(registry, pipeline=boom, webhooks=emitter)
            state = manager.wait(
                manager.start(meeting, origin="console", actor="console").id, timeout=10
            )
            assert state.status == "failed"
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    types = _wait_for_events(receiver, ["run.started", "run.failed"])
    assert types == ["run.started", "run.failed"]


def test_delivery_failure_leaves_the_runs_own_status_untouched(tmp_path) -> None:
    """The core promise: a broken endpoint must not change the run's outcome."""
    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])

    def pipeline(directory, options, on_event) -> None:
        (Path(directory) / "segments.json").write_text("{}", encoding="utf-8")

    with _Receiver(status=500) as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], max_attempts=3, backoff_base=0.001
        )
        try:
            manager = RunManager(registry, pipeline=pipeline, webhooks=emitter)
            state = manager.wait(
                manager.start(meeting, origin="console", actor="console").id, timeout=10
            )
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    assert state.status == "done"
    assert state.error is None
    assert registry.get_run(state.run_id).status == "done"
    assert registry.meeting_by_id(meeting.id).status == "recorded"
    # Every event failed to deliver, and yet the run is untouched.
    assert emitter.deliveries()
    assert all(delivery.status == "failed" for delivery in emitter.deliveries())


# --- archive events --------------------------------------------------------- #


def test_archive_created_is_emitted_after_a_complete_archive(tmp_path) -> None:
    registry, meeting = _seeded_archive(tmp_path)
    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], backoff_base=0.001
        )
        try:
            archive = archive_meeting(
                registry,
                meeting,
                webhooks=emitter,
                actor="console",
            )
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    assert Path(archive.root_path).is_dir()
    payload = json.loads(receiver.requests[0][1])
    assert payload["type"] == "archive.created"
    assert payload["project_id"] == meeting.project_id
    assert payload["meeting_id"] == meeting.id
    assert payload["event"]


def test_a_failed_archive_emits_nothing(tmp_path) -> None:
    registry, meeting = _seeded_archive(tmp_path)
    with _Receiver() as receiver:
        emitter = WebhookEmitter(
            [WebhookEndpoint(url=receiver.url)], backoff_base=0.001
        )
        try:
            (Path(meeting.workspace_path) / "record.json").unlink()
            # The registry still lists the artifact, so the copy fails.
            with pytest.raises(FileNotFoundError):
                archive_meeting(
                    registry,
                    meeting,
                    webhooks=emitter,
                    actor="console",
                )
        finally:
            emitter.close(timeout=5)
    assert receiver.requests == []


# --- configuration (ADR-0007 precedence) ------------------------------------ #

_CONFIG = """\
[[webhooks.endpoints]]
url = "http://config.test/hook"
events = ["run.finished"]
include_content = true
secret_env = "CR_CONFIG_SECRET"
"""


def test_config_file_endpoints_are_parsed(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    endpoints = resolve_endpoints(environ={"CR_CONFIG_SECRET": "s"}, config_file=config)
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    assert endpoint.url == "http://config.test/hook"
    assert endpoint.events == ("run.finished",)
    assert endpoint.include_content is True
    assert endpoint.secret_env == "CR_CONFIG_SECRET"
    assert endpoint.wants("run.finished")
    assert not endpoint.wants("run.started")


def test_env_beats_config_and_explicit_beats_env(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    env = {"CR_WEBHOOKS": json.dumps([{"url": "http://env.test/hook"}])}

    from_env = resolve_endpoints(environ=env, config_file=config)
    assert [e.url for e in from_env] == ["http://env.test/hook"]

    explicit = resolve_endpoints(
        [{"url": "http://explicit.test/hook"}], environ=env, config_file=config
    )
    assert [e.url for e in explicit] == ["http://explicit.test/hook"]


def test_absent_or_malformed_config_means_no_endpoints(tmp_path) -> None:
    assert resolve_endpoints(environ={}, config_file=tmp_path / "missing.toml") == ()
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not toml = = =", encoding="utf-8")
    assert resolve_endpoints(environ={}, config_file=broken) == ()
    no_table = tmp_path / "no-table.toml"
    no_table.write_text('[paths]\ndata_dir = "~/x"\n', encoding="utf-8")
    assert resolve_endpoints(environ={}, config_file=no_table) == ()


def test_a_typo_in_the_event_filter_is_surfaced_and_skipped(capsys) -> None:
    config = load_webhook_config(
        [{"url": "http://x.test", "events": ["run.finishd"]}],
        environ={},
        config_file=None,
    )
    assert config.endpoints == ()
    assert any("unknown event" in problem for problem in config.problems)
    assert not config.ok
    assert "unknown event" in capsys.readouterr().err


def test_a_malformed_env_value_is_surfaced_not_raised(capsys) -> None:
    config = load_webhook_config(environ={"CR_WEBHOOKS": "{not json"}, config_file=None)
    assert config.endpoints == ()
    assert any("not valid JSON" in problem for problem in config.problems)

    wrong_shape = load_webhook_config(
        environ={"CR_WEBHOOKS": '{"url": "http://x"}'}, config_file=None
    )
    assert wrong_shape.endpoints == ()
    assert any("JSON array" in problem for problem in wrong_shape.problems)
    assert "webhook config" in capsys.readouterr().err


def test_a_malformed_config_is_surfaced_but_never_raises(tmp_path, capsys) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("this is not toml = = =", encoding="utf-8")
    config = load_webhook_config(environ={}, config_file=broken)
    assert config.endpoints == ()
    assert config.problems
    assert not config.ok
    err = capsys.readouterr().err
    assert "clear-record: webhook config:" in err
    assert str(broken) in err and "not valid TOML" in err


def test_a_bad_config_still_lets_a_run_complete(tmp_path, capsys) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text(
        '[[webhooks.endpoints]]\nurl = "http://x.test"\nevents = ["bogus"]\n',
        encoding="utf-8",
    )
    emitter = WebhookEmitter.from_config(environ={}, config_file=bad)
    assert emitter.enabled is False
    assert emitter.problems

    registry = _registry(tmp_path)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = _meeting(registry, tmp_path, [tape])
    manager = RunManager(registry, pipeline=lambda *args: None, webhooks=emitter)
    state = manager.wait(
        manager.start(meeting, origin="console", actor="console").id, timeout=10
    )

    assert state.status == "done"
    assert registry.get_run(state.run_id).status == "done"
    assert "unknown event" in capsys.readouterr().err


def test_an_endpoint_with_an_unset_secret_is_surfaced_and_delivers_nothing(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MISSING_SECRET", raising=False)
    with _Receiver() as receiver:
        emitter = WebhookEmitter.from_config(
            [{"url": receiver.url, "secret_env": "CR_MISSING_SECRET"}],
            environ={},
            config_file=None,
            backoff_base=0.001,
        )
        try:
            # Surfaced at config time...
            assert emitter.problems
            assert any("CR_MISSING_SECRET" in p for p in emitter.problems)
            assert emitter.emit("run.finished", project_id=1) is not None
            assert emitter.flush(timeout=5)
        finally:
            emitter.close(timeout=5)

    assert receiver.requests == []  # nothing sent unsigned
    assert emitter.deliveries()[0].status == "failed"
    # ...and again, actionable, when an event is actually dropped.
    assert "refusing to send unsigned" in capsys.readouterr().err


def test_default_emitter_disables_delivery_on_a_bad_config(
    tmp_path, monkeypatch
) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text(
        '[[webhooks.endpoints]]\nurl = "http://x.test"\nevents = ["bogus"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(webhooks_module, "config_path", lambda: bad)
    webhooks_module.reset_default_emitter()
    try:
        emitter = webhooks_module.default_emitter()
        assert emitter.enabled is False
        assert emitter.problems
        assert emitter.emit("run.started") is None
    finally:
        webhooks_module.reset_default_emitter()


def test_default_emitter_is_config_driven(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("CR_CONFIG_SECRET", "s3cret")
    monkeypatch.setattr(webhooks_module, "config_path", lambda: config)
    webhooks_module.reset_default_emitter()
    try:
        emitter = webhooks_module.default_emitter()
        assert emitter.enabled is True
        assert emitter.endpoints[0].url == "http://config.test/hook"
        assert emitter.problems == ()
    finally:
        webhooks_module.reset_default_emitter()
