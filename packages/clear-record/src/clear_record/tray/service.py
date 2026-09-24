"""Supervise the local console as an in-process background server.

Deliberately **Qt-free**: the tray icon is a thin shell over this, so the
supervision logic (start, wait-until-ready, live state, stop, restart) is
testable without a display — and reusable by a future
`clear-record serve --supervise` on a headless node.

Starting the server here is starting a **node**: the server records the address
it bound where every surface resolves it (:mod:`clear_record.core.node`), and
the health probe below reaches it through the same one client the command line,
the console and the MCP adapter use — so the tray's liveness signal and their
"where is the node" answer cannot drift.
"""

from __future__ import annotations

import enum
import threading

from clear_record.core import node
from clear_record.service import Registry
from clear_record.web.app import NodeServer, create_app


class ServiceState(enum.StrEnum):
    """What the supervised server is doing **right now**.

    ``STOPPED`` — no supervisor thread is alive (never started, stopped, or the
    server thread died, e.g. the port was taken). ``RUNNING`` — the thread is
    alive and the health endpoint answers. ``UNREACHABLE`` — the thread is alive
    but the health endpoint does not answer (still starting, or wedged).
    """

    STOPPED = "stopped"
    RUNNING = "running"
    UNREACHABLE = "unreachable"


class ServiceController:
    """Runs the console on a background thread and can stop it cleanly."""

    def __init__(
        self,
        *,
        host: str = node.DEFAULT_HOST,
        port: int = node.DEFAULT_PORT,
        data_dir: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> node.NodeAddress:
        """Where the node this controller started answers — the record, once bound.

        The record is the address every surface resolves, and the only one that
        names a node started on an ephemeral port (``--port 0``), so the health
        probe dials what the node actually holds rather than the port it asked
        for. Until the server this controller started has finished binding there
        is no record of ours to read and the requested ``host:port`` stands —
        which is also the honest answer for a port we never bound (the server
        died, e.g. the port was taken: nothing answers there).
        """
        bound = self._server is not None and bool(
            getattr(self._server, "started", False)
        )
        recorded = node.recorded() if bound else None
        return recorded or node.NodeAddress(self.host, self.port)

    @property
    def url(self) -> str:
        return self.address.url

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        import uvicorn

        if self.running:
            return
        app = create_app(Registry.open(data_dir=self.data_dir))
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning"
        )
        # NodeServer records the bound address while it listens, so this posture
        # publishes its address exactly as `clear-record serve` does.
        self._server = NodeServer(config)
        app.state.server = self._server
        self._thread = threading.Thread(
            target=self._server.run, name="cr-console", daemon=True
        )
        self._thread.start()

    def wait_until_ready(self, timeout: float = 15.0, interval: float = 0.1) -> bool:
        """Poll the health endpoint until the server answers or ``timeout``."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False  # the server died (e.g. the port is taken)
            time.sleep(interval)
        return self.healthy()

    def healthy(self) -> bool:
        """Does the console's health URL answer exactly 200, right now?

        Reached through the one node client (:func:`clear_record.core.node.reach`),
        so the tray probes the node exactly as every other surface reaches it.
        Only a 200 counts and redirects are **not** followed: a 3xx (e.g. a
        redirect to the setup page) is not a healthy server. The path stays the
        current one — B3 replaces it with a dedicated credential-free
        ``GET /health`` (AUTH-06/AUTH-07), whose response must not carry the
        registry path.
        """
        try:
            node.reach(self.address)
        except node.NoNodeError:
            return False
        return True

    def state(self) -> ServiceState:
        """The live state, probed now — never a start-time snapshot.

        The tray reads this on a timer, so it must stay cheap and must not
        cache: a server that died after startup has to read as stopped. The
        health probe is the only liveness signal, so it has to remain
        answerable without a credential (see the health route).
        """
        if not self.running:
            return ServiceState.STOPPED
        return ServiceState.RUNNING if self.healthy() else ServiceState.UNREACHABLE

    def stop(self, timeout: float = 10.0) -> bool:
        """Ask the server to exit and wait for its thread; True when it is gone.

        A graceful shutdown waits for in-flight requests (a transcription takes
        a while), so the join may time out. The handles are then **kept**:
        clearing them would report "stopped" — and let :meth:`start` bind a
        second server on the port the first still holds. A later
        :meth:`stop` retries the join.
        """
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is None:
            self._server = None
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            return False
        self._thread = None
        self._server = None
        return True

    def restart(self, timeout: float = 15.0) -> bool:
        """Stop the server, then start it again on the same URL.

        Returns False when the old server did not shut down in time: nothing is
        started (a second bind on the same port would fail), the handles stay,
        and the caller can report the failure and retry.
        """
        if not self.stop(timeout):
            return False
        self.start()
        return self.wait_until_ready(timeout=timeout)


__all__ = ["ServiceController", "ServiceState"]
