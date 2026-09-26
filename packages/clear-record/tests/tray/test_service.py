"""The supervisor behind the tray icon: start, readiness, live state, restart, stop.

No Qt and no display — this is the part worth testing. The two awkward paths
are driven with stubs instead of sleeps: a shutdown that outlasts the join
timeout, and a health route that answers with a redirect.

What the tray *is* is the other half of that: a client of a node that may already
exist. So the tests below drive both postures — one it started, and one it only
**joined** where the record says a node answers — with the same attention, because
"no second node is started" and "the status is the node's health" are contracts
about a controller that runs no server of its own.
"""

from __future__ import annotations

import http.server
import socket
import threading
import urllib.request

import pytest
from clear_record.core import node
from clear_record.tray.app import status_text
from clear_record.tray.service import ServiceController, ServiceState


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Node(http.server.BaseHTTPRequestHandler):
    """A listener that answers the node's health path exactly as a node does."""

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep the test output quiet


class _AnsweringNode:
    """A node that is already there: recorded where surfaces resolve it, no child here.

    The case :meth:`ServiceController.start` attaches to. A real ``serve`` posture
    would be more machinery for the same three facts this stub carries — the
    record, one request against it, one health answer — and that posture is what
    ``tests/test_node_address.py`` drives.
    """

    def __init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Node)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="answering-node", daemon=True
        )
        self._thread.start()
        self.address = node.NodeAddress.of("127.0.0.1", self._server.server_address[1])
        node.record(self.address)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


@pytest.fixture
def answering_node():
    """A node already listening where the record says, for the whole test."""
    joined = _AnsweringNode()
    try:
        yield joined
    finally:
        joined.stop()
        node.forget()


class _StubbornThread:
    """A supervisor thread that never finishes inside the join timeout.

    Stubbed on purpose: a real in-flight request would need a slow route and
    sleeps, and the contract under test is ``stop``'s, not uvicorn's.
    """

    def __init__(self) -> None:
        self.joins = 0

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.joins += 1


class _StubServer:
    """The uvicorn server object ``stop`` signals and then drops."""

    def __init__(self) -> None:
        self.should_exit = False


class _Redirecting(http.server.BaseHTTPRequestHandler):
    """Redirects the health path to the setup page, which answers 200."""

    def do_GET(self) -> None:
        if self.path == "/web/setup":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"setup")
            return
        self.send_response(307)
        self.send_header("Location", "/web/setup")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass  # keep the test output quiet


def test_controller_serves_health_and_stops(tmp_path) -> None:
    port = _free_port()
    controller = ServiceController(port=port, data_dir=str(tmp_path))

    assert not controller.healthy()  # nothing is listening yet
    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "server did not become ready"
        assert controller.running
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5
        ) as response:
            assert b'"status":"ok"' in response.read()
    finally:
        controller.stop(timeout=15)

    assert not controller.running


def test_stop_is_idempotent(tmp_path) -> None:
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    assert controller.stop() is True  # never started: must not raise
    controller.start()
    assert controller.stop(timeout=15) is True
    assert controller.stop(timeout=15) is True


def test_state_is_read_live_not_snapshotted(tmp_path) -> None:
    """The tray polls this, so it must follow the server, not memory."""
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    assert controller.state() is ServiceState.STOPPED  # never started

    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "server did not become ready"
        assert controller.state() is ServiceState.RUNNING
    finally:
        controller.stop(timeout=15)

    assert controller.state() is ServiceState.STOPPED


def test_state_is_unreachable_while_the_thread_is_alive(tmp_path, monkeypatch) -> None:
    """A live process whose health endpoint stops answering is not "running"."""
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "server did not become ready"
        monkeypatch.setattr(controller, "healthy", lambda: False)
        assert controller.state() is ServiceState.UNREACHABLE
    finally:
        controller.stop(timeout=15)


def test_restart_serves_the_same_url_again(tmp_path) -> None:
    port = _free_port()
    controller = ServiceController(port=port, data_dir=str(tmp_path))
    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "server did not become ready"
        assert controller.restart(timeout=15), "server did not come back"
        assert controller.state() is ServiceState.RUNNING
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5
        ) as response:
            assert response.status == 200
    finally:
        controller.stop(timeout=15)


def test_stop_that_does_not_join_keeps_the_handle_and_refuses_to_restart(
    tmp_path, monkeypatch
) -> None:
    """A graceful shutdown can outlast the timeout (an in-flight request).

    Clearing the handles anyway would report "stopped" — and then restart()
    would bind a second server on a port the first still holds.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    thread = _StubbornThread()
    server = _StubServer()
    controller._thread = thread
    controller._server = server
    monkeypatch.setattr(controller, "healthy", lambda: True)

    assert controller.stop(timeout=0.01) is False
    assert server.should_exit is True  # the shutdown was asked for
    assert thread.joins == 1
    assert controller.running is True  # ... but the handle is kept
    assert controller._thread is thread
    assert controller._server is server
    assert controller.state() is ServiceState.RUNNING  # health still answers

    assert controller.restart(timeout=0.01) is False  # no second bind
    assert controller._thread is thread
    assert thread.joins == 2  # the failed restart asked it to stop again

    controller.start()  # a direct start is a no-op while it is alive
    assert controller._thread is thread


def test_a_redirect_is_not_a_healthy_server(tmp_path) -> None:
    """A 3xx must not read as healthy: the probe must not follow it to a 200 page."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        controller = ServiceController(
            port=server.server_address[1], data_dir=str(tmp_path)
        )
        assert not controller.healthy()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_start_joins_the_node_that_answers_instead_of_starting_a_second(
    tmp_path, answering_node
) -> None:
    """A node already up is the node this tray serves; it starts none of its own.

    The controller is given a free port of its own — and a tray that bound it would
    publish a second address on the machine, so the record is what says which node
    this is. Everything it reports is then *that* node's: its address, its URL, its
    health, and no thread here at all.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path / "tray"))
    controller.start()
    try:
        assert node.ask() == answering_node.address, "the record still names that node"
        assert controller.address == answering_node.address
        assert controller.url == answering_node.address.url
        assert controller.state() is ServiceState.RUNNING
        assert controller.supervises is False, "the tray started no node"
        assert controller.running is False, "... and runs no server of its own"
    finally:
        assert controller.stop(timeout=15) is True
    assert node.ask() == answering_node.address, "quitting left it running"


def test_the_status_follows_the_node_it_joined_not_a_thread_of_its_own(
    tmp_path, answering_node
) -> None:
    """The status line is the node's health — a joined node has no child to read.

    A status decided by this process's thread would call a node that *is*
    answering "stopped", and would keep saying "running" after that node went
    away. Both readings are the node's own answer, which is the same question
    whether the tray started it or joined it.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path / "tray"))
    controller.start()
    try:
        assert status_text(controller) == f"Running at {answering_node.address.url}"
        answering_node.stop()
        assert not controller.healthy()
        assert controller.state() is ServiceState.UNREACHABLE
        assert status_text(controller) == "Not responding"
    finally:
        assert controller.stop(timeout=15) is True


def test_a_node_the_tray_only_joined_is_not_stopped_or_restarted_here(
    tmp_path, answering_node
) -> None:
    """A node the user started is not this tray's to stop: quitting leaves it up.

    ``restart`` answers ``False`` and starts nothing — the node is restarted by
    whoever runs it, and a second node is what the join avoids.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path / "tray"))
    controller.start()
    try:
        assert controller.restart() is False, "the node is not this tray's to restart"
        assert controller.supervises is False
        assert controller.running is False
        assert node.ask() == answering_node.address, "and it was left alone"
    finally:
        assert controller.stop(timeout=15) is True
    assert node.ask() == answering_node.address, "quitting left it running"


def test_a_tray_that_joined_a_node_that_stopped_can_start_one_again(
    tmp_path, answering_node
) -> None:
    """The way back for a tray whose joined node went away is a **click**, not the tick.

    Once the node the tray joined stops, the tray is a client of nothing, and
    ``restart`` — the owner's act — still answers ``False``. So the item it offers
    has to be able to start a node of its own, exactly as ``start`` does,
    re-reading the record; otherwise the tray is a dead handle until relaunched.
    The item is offered in that state and takes the user's click as the ask: the
    poll tick only reads, so nothing starts on its own.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path / "tray"))
    controller.start()
    try:
        assert controller.supervises is False, "the tray joined that node"
        answering_node.stop()
        assert controller.state() is ServiceState.UNREACHABLE
        assert controller.restart() is False, "restart stays the owner's act"
        assert controller.offers_restart() is True, "but the item must be offered"
        assert controller.restart_or_start(timeout=15), "the click did not start one"
        assert controller.supervises is True, "the tray now owns a node"
        assert controller.state() is ServiceState.RUNNING
    finally:
        assert controller.stop(timeout=15) is True


def test_the_status_line_and_the_restart_offer_share_one_live_read(
    tmp_path, monkeypatch
) -> None:
    """One poll tick reads the node's health once, and both readers use that read.

    The tick shows the status line and decides whether to offer a restart from the
    same live state; were each to read the node again, a node that accepts but does
    not answer would cost the whole poll interval twice. A pre-read ``state``
    therefore reaches both consumers without a second probe.
    """
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    reads: list[ServiceState] = []

    def counted() -> ServiceState:
        reads.append(ServiceState.UNREACHABLE)
        return ServiceState.UNREACHABLE

    monkeypatch.setattr(controller, "state", counted)

    live = controller.state()  # what refresh reads once
    assert status_text(controller, state=live) == "Not responding"
    assert controller.offers_restart(state=live) is True
    assert len(reads) == 1, "a reader probed the node again"


def test_the_trays_own_node_publishes_the_local_session(tmp_path, monkeypatch) -> None:
    """The tray's posture is a node: the command line is not asked to sign in.

    The tray starts its node **in this process** rather than through ``serve``, so
    the session the command line presents has to belong to the server every
    posture runs (:class:`~clear_record.web.app.NodeServer`) rather than to one
    entry point — otherwise the desktop posture, where the tray *is* the node,
    refuses the operator's own `clear-record run`.
    """
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path / "data"))
    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "the tray's node never came up"
        assert node.local_session(), "the tray's node published no local session"

        address = node.ask()  # the recorded node, probed anonymously
        answer = node.request(address, "POST", "/api/runs", {})

        # Past the gate: the shape of the body is what refuses this, not a session.
        assert answer.status == 422, answer.detail()
        assert _anonymous_status(address, "/api/projects") == 401
    finally:
        controller.stop(timeout=15)

    assert node.local_session() is None, "the session outlived the node"


def _anonymous_status(address: node.NodeAddress, path: str) -> int:
    """One request with no cookie at all, as a client that holds none makes it."""
    try:
        with urllib.request.urlopen(address.url_for(path), timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
