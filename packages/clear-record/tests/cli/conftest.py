"""A node in this process, for the tests that exercise a surface as its client.

ADR-0032 makes the command line a client of a node, so a test of its run path
needs a node to be a client *of*: this fixture brings up a real app — uvicorn on
an ephemeral port, serving the app object the console's routes serve — over a
registry the test can read directly, and records its address where every surface
resolves it (``core.node``, the same one ``serve`` and ``web`` publish).

What stays real is the whole relation: the HTTP edge, the queue, the claim, the
registry rows and the console's own reads. What a test replaces, if it wants to,
is the pipeline (``pipeline=``) — the suite's ASR backend is faked, a run is not.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from clear_record.core import node
from clear_record.service import Registry, RunManager
from clear_record.web.app import create_app

#: How long a node gets to answer before a test calls it a bug.
READY_TIMEOUT = 20.0


def free_port() -> int:
    """A port nothing is listening on (bound and released, as tests do)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def node_in_this_process(tmp_path):
    """A factory: bring a node up here, and take it down at the end of the block.

    ``pipeline`` overrides the run manager's pipeline (a test that only needs the
    queue gates a fake); by default the node runs the real one. ``root`` is where
    its registry lives — the fixture's ``tmp_path`` unless the caller names one.

    Yields a context manager returning a namespace: ``address`` (what the node
    recorded), ``registry`` and ``manager`` (the node's own), ``console`` — a
    ``TestClient`` over the *same app object*, so what the console shows is
    exactly what the node owns — and ``server``, the uvicorn server itself, so a
    test can take the node **away** under a client that is already talking to it
    (``should_exit = True``) and watch what that client does.
    """

    @contextlib.contextmanager
    def bring_up(*, pipeline=None, root=None) -> Iterator[SimpleNamespace]:
        home = tmp_path if root is None else root
        registry = Registry.open(db_path=home / "registry.sqlite3")
        manager = (
            RunManager(registry, pipeline=pipeline)
            if pipeline is not None
            else RunManager(registry)
        )
        app = create_app(registry, runs=manager, trusted_hosts=("testserver",))
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, name="test-node", daemon=True)
        thread.start()
        address = node.NodeAddress.of("127.0.0.1", port)
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            try:
                node.reach(address, timeout=1.0)
                break
            except node.NoNodeError:
                if time.monotonic() > deadline:
                    pytest.fail("the node never answered")
                time.sleep(0.02)
        # Every posture records where it bound; this one does the same, so the
        # surfaces under test resolve it exactly as they resolve `serve`'s.
        node.record(address)
        try:
            yield SimpleNamespace(
                address=address,
                registry=registry,
                manager=manager,
                console=TestClient(app),
                server=server,
            )
        finally:
            node.forget()
            server.should_exit = True
            thread.join(READY_TIMEOUT)
            assert not thread.is_alive(), "the node did not stop"

    return bring_up
