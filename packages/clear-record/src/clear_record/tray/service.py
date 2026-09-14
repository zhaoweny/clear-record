"""Supervise the local console as an in-process background server.

Deliberately **Qt-free**: the tray icon is a thin shell over this, so the
supervision logic (start, wait-until-ready, stop) is testable without a display
— and reusable by a future `clear-record serve --supervise` on a headless node.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

from clear_record.service import Registry
from clear_record.web.app import create_app


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

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._server = None


__all__ = ["ServiceController"]
