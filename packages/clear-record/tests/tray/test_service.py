"""The supervisor behind the tray icon: start, readiness, health, stop.

No Qt and no display — this is the part worth testing.
"""

from __future__ import annotations

import socket
import urllib.request

from clear_record.tray.service import ServiceController


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_controller_serves_health_and_stops(tmp_path) -> None:
    port = _free_port()
    controller = ServiceController(port=port, data_dir=str(tmp_path))

    assert not controller.healthy()  # nothing is listening yet
    controller.start()
    try:
        assert controller.wait_until_ready(timeout=15), "server did not become ready"
        assert controller.running
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=5
        ) as response:
            assert b'"status":"ok"' in response.read()
    finally:
        controller.stop(timeout=15)

    assert not controller.running


def test_stop_is_idempotent(tmp_path) -> None:
    controller = ServiceController(port=_free_port(), data_dir=str(tmp_path))
    controller.stop()  # never started: must not raise
    controller.start()
    controller.stop(timeout=15)
    controller.stop(timeout=15)
