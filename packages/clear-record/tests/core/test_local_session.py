"""The session a node publishes for the surfaces on its own machine.

The node's own operating-system user is inside the boundary the auth gate
defends (ADR-0033), so the command line is not asked for a password: the node
opens one session for that user and publishes its token beside the address
record, and a client on the machine presents it as the session cookie. Two things
these tests hold to, both from the seam's own constraints:

- the file is the node's own — ``0600``, in the state directory, removed on a
  clean exit, and **total** to read (a missing, empty or hand-edited file is no
  token, never an exception);
- the token travels **only to the node that recorded the address** — a client
  dialling anything else is asked anonymously, so a token this machine's node
  issued is never handed to another listener.
"""

from __future__ import annotations

import http.server
import os
import threading
from collections.abc import Iterator

import pytest
from clear_record.core import node

#: What the file may hold: the token and a newline, and nothing else.
TOKEN = "a-local-session-token-for-the-test"


@pytest.fixture(autouse=True)
def _own_state_dir(tmp_path, monkeypatch) -> None:
    """Keep the node's own state directory inside the test."""
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))


class _Capturing(http.server.BaseHTTPRequestHandler):
    """A listener that answers 200 and remembers the Cookie it was sent."""

    seen: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        type(self).seen.append(self.headers.get("Cookie", ""))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass  # keep the test output quiet


def _listener() -> tuple[http.server.ThreadingHTTPServer, node.NodeAddress]:
    """A listener on a free loopback port, and the address that dials it."""
    listener = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Capturing)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    return listener, node.NodeAddress.of("127.0.0.1", listener.server_address[1])


def _stop(listener) -> None:
    listener.shutdown()
    listener.server_close()


# --- the file --------------------------------------------------------------- #


def test_publishing_writes_the_token_for_this_user_alone(tmp_path) -> None:
    """Mode ``0600`` at creation, the token and nothing else, gone on request."""
    node.record(node.NodeAddress.of("127.0.0.1", 8765))  # what a node does first
    path = node.publish_local_session(TOKEN)

    assert path == node.local_session_path()
    assert path.read_text(encoding="utf-8") == TOKEN + "\n"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert node.local_session() == TOKEN

    node.forget_local_session()

    assert node.local_session() is None
    assert not path.exists()
    node.forget_local_session()  # a second call on a file that is not there
    node.forget()


def test_reading_the_file_is_total(tmp_path) -> None:
    """Nothing about the file can raise or invent a token.

    A token is one line and nothing else, so a hand-edited file that carries a
    second line or an interior space is no token: read as one, it would reach the
    HTTP client and the request would fail to build — and a surface would report
    an answering node as "no node is listening".
    """
    assert node.local_session() is None  # never written

    node.local_session_path().parent.mkdir(parents=True, exist_ok=True)
    for written in ("", "   \n", "\n\n", f"{TOKEN}\nsecond line\n", "tok en\n"):
        node.local_session_path().write_text(written, encoding="utf-8")
        assert node.local_session() is None, repr(written)


def test_forgetting_leaves_the_session_file_another_node_published() -> None:
    """The guard :func:`forget` makes, applied to the session file beside it.

    Two nodes share a state directory (the tray's supervised node and a ``serve``)
    and the address record names the one that holds it, so a node shutting down
    has no business erasing the file its successor published: the record is the
    test, exactly as it is for the address itself.
    """
    node.record(node.NodeAddress(host="127.0.0.1", port=8765, pid=os.getpid() + 1))
    node.publish_local_session("a-token-the-other-node-published")

    node.forget_local_session()

    assert node.local_session() == "a-token-the-other-node-published"
    node.forget()


def test_publishing_replaces_the_previous_token(tmp_path) -> None:
    """A rewritten file is one token, never half of two."""
    node.publish_local_session(TOKEN)
    node.publish_local_session("the-fresh-token")

    assert node.local_session() == "the-fresh-token"
    assert sorted(p.name for p in node.local_session_path().parent.iterdir()) == [
        node.LOCAL_SESSION_FILENAME
    ]


# --- where the token travels ------------------------------------------------- #


def test_a_client_presents_the_token_only_to_the_recorded_node() -> None:
    """The recorded node is asked with the cookie; any other listener anonymously."""
    recorded_listener, recorded_address = _listener()
    stranger, stranger_address = _listener()
    try:
        node.record(recorded_address)
        node.publish_local_session(TOKEN)

        node.request(recorded_address, "GET", "/web/")
        node.request(stranger_address, "GET", "/web/")

        assert _Capturing.seen == [f"{node.SESSION_COOKIE}={TOKEN}", ""]
    finally:
        node.forget_local_session()
        node.forget()
        _stop(recorded_listener)
        _stop(stranger)


def test_a_client_with_no_published_session_sends_no_cookie() -> None:
    listener, address = _listener()
    try:
        node.record(address)

        node.request(address, "GET", "/web/")

        assert _Capturing.seen == [""]
    finally:
        node.forget()
        _stop(listener)


def test_a_hand_edited_file_is_asked_anonymously_not_as_a_broken_cookie() -> None:
    """A file holding a token *and* something else is no cookie, not an error.

    Read as one, the value would carry a newline into the request and the client
    would fail to build it — reporting an answering node as "no clear-record node
    is listening". The reader refuses the shape first, so the request goes out
    with no cookie and is answered exactly as any other anonymous one.
    """
    listener, address = _listener()
    try:
        node.record(address)
        node.local_session_path().parent.mkdir(parents=True, exist_ok=True)
        node.local_session_path().write_text(
            f"{TOKEN}\nsecond line\n", encoding="utf-8"
        )
        assert node.local_session() is None

        node.request(address, "GET", "/web/")

        assert _Capturing.seen == [""]
    finally:
        node.forget_local_session()
        node.forget()
        _stop(listener)


@pytest.fixture(autouse=True)
def _clean_capture() -> Iterator[None]:
    _Capturing.seen = []
    yield
    _Capturing.seen = []


def test_the_liveness_read_carries_no_local_session() -> None:
    """A health probe never puts a live token on the wire; a gated call does.

    ``reach``/``ask`` are the read every surface makes to find out whether a node
    is there, and the liveness route answers anonymously — so a token on that
    request would be a live session handed to whatever the address record names,
    including a stranger's listener, for an answer that needed no credential.
    """
    listener, address = _listener()
    try:
        node.record(address)
        node.publish_local_session(TOKEN)

        node.reach(address, "/health")
        node.ask()

        assert _Capturing.seen == ["", ""], "a liveness read carried the session"

        node.request(address, "GET", "/api/v1/projects")

        assert _Capturing.seen[-1] == f"{node.SESSION_COOKIE}={TOKEN}"
    finally:
        node.forget_local_session()
        node.forget()
        _stop(listener)
