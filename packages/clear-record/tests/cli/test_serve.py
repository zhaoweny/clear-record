"""The ``clear-record serve`` entry point: headless, sink logging, clean stop.

``serve`` and ``web`` are two postures of one console (ADR-0013): the
interactive one opens a browser and logs to the terminal; the node one is
headless, sends its logs to the diagnostics sink, and is what a systemd/launchd
unit runs. These tests pin those differences without starting a real server.
"""

from __future__ import annotations

from click.testing import CliRunner

from clear_record.cli import cli


def _invoke(monkeypatch, argv: list[str]) -> dict:
    """Invoke a console command with ``web._run`` replaced by a recorder."""
    import clear_record.web as web

    captured: dict = {}

    def fake_run(**kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(web, "_run", fake_run)
    result = CliRunner().invoke(cli._build_group(), argv)
    assert result.exit_code == 0, result.output
    return captured


def test_serve_is_registered_and_headless(monkeypatch) -> None:
    captured = _invoke(monkeypatch, ["serve", "--port", "9999"])
    assert captured["port"] == 9999
    assert captured["no_browser"] is True
    assert captured["service"] is True


def test_web_stays_interactive(monkeypatch) -> None:
    captured = _invoke(monkeypatch, ["web", "--port", "9999"])
    assert captured["no_browser"] is False
    assert captured.get("service", False) is False


def test_serve_sends_console_logs_to_the_diagnostics_sink(
    monkeypatch, tmp_path
) -> None:
    """`serve` passes a uvicorn log config that routes its loggers to the sink."""
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    captured: dict = {}

    def fake_serve(**kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("clear_record.web.app.serve", fake_serve)
    result = CliRunner().invoke(cli._build_group(), ["serve", "--port", "9999"])
    assert result.exit_code == 0, result.output

    config = captured["log_config"]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["handlers"] == ["diagnostics"]

    # The interactive command keeps uvicorn's own logging.
    captured.clear()
    result = CliRunner().invoke(cli._build_group(), ["web", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert "log_config" not in captured


def test_a_stopped_server_stops_the_run_queue(monkeypatch, tmp_path) -> None:
    """`serve` always stops the queue when uvicorn returns (clean SIGTERM)."""
    import uvicorn

    from clear_record.service.runs import RunManager
    from clear_record.web import app as web_app

    calls: list[int] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
    monkeypatch.setattr(
        RunManager, "shutdown", lambda self, timeout=None: calls.append(1)
    )

    assert (
        web_app.serve(
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=str(tmp_path),
        )
        == 0
    )
    assert calls == [1]


def test_the_queue_stops_even_when_the_server_errors(monkeypatch, tmp_path) -> None:
    import pytest
    import uvicorn

    from clear_record.service.runs import RunManager
    from clear_record.web import app as web_app

    calls: list[int] = []

    def boom(self) -> None:
        raise RuntimeError("server exploded")

    monkeypatch.setattr(uvicorn.Server, "run", boom)
    monkeypatch.setattr(
        RunManager, "shutdown", lambda self, timeout=None: calls.append(1)
    )

    with pytest.raises(RuntimeError):
        web_app.serve(
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=str(tmp_path),
        )
    assert calls == [1]
