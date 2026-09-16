"""Supervise the local console as an in-process background server.

Deliberately **Qt-free**: the tray icon is a thin shell over this, so the
supervision logic (start, wait-until-ready, live state, stop, restart) is
testable without a display — and reusable by a future
`clear-record serve --supervise` on a headless node.
"""

from __future__ import annotations

import enum
import threading
import urllib.error
import urllib.request

from clear_record.service import Registry
from clear_record.web.app import create_app


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
        host: str = "127.0.0.1",
        port: int = 8765,
        data_dir: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

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
        self._server = uvicorn.Server(config)
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
        try:
            with urllib.request.urlopen(
                f"{self.url}api/health", timeout=1.0
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

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

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._server = None

    def restart(self, timeout: float = 15.0) -> bool:
        """Stop the server and start it again on the same URL.

        Blocks until the old server releases the port (typically well under a
        second; ``timeout`` bounds a wedged one), so the caller is the tray's
        explicit restart action, not the poll timer. Returns whether the
        console answered again before ``timeout``.
        """
        self.stop(timeout)
        self.start()
        return self.wait_until_ready(timeout=timeout)


__all__ = ["ServiceController", "ServiceState"]
