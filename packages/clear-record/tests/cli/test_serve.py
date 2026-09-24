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


def test_serve_carries_supervise_and_no_other_verb_carries_it(monkeypatch) -> None:
    """`--supervise` is a flag on `serve`, and on nothing else."""
    captured: dict = {}

    def fake_serve(**kwargs) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("clear_record.web.app.serve", fake_serve)

    result = CliRunner().invoke(cli._build_group(), ["serve", "--supervise"])
    assert result.exit_code == 0, result.output
    assert captured["supervise"] is True

    captured.clear()
    result = CliRunner().invoke(cli._build_group(), ["serve"])
    assert result.exit_code == 0, result.output
    assert "supervise" not in captured

    captured.clear()
    result = CliRunner().invoke(cli._build_group(), ["web", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert "supervise" not in captured


def test_serve_supervise_starts_the_node_again_after_a_crash(
    monkeypatch, tmp_path, capsys
) -> None:
    """What `--supervise` documents: a server that stops unasked is started again."""
    import uvicorn

    from clear_record.web import app as web_app

    runs: list[int] = []

    def crashing_run(self) -> None:
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("server exploded")
        self.should_exit = True  # the second server is then asked to stop

    monkeypatch.setattr(uvicorn.Server, "run", crashing_run)
    monkeypatch.setattr(web_app, "_RESTART_PAUSE", 0.0)

    assert (
        web_app.serve(
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=str(tmp_path),
            supervise=True,
        )
        == 0
    )
    assert runs == [1, 1], "the crashed node was started again"
    # The traceback reaches the node's own log rather than being swallowed: here
    # that is uvicorn's stderr config, and under `serve` it is the sink.
    assert "the node stopped with an error" in capsys.readouterr().err


def test_serve_supervise_ends_when_the_node_is_asked_to_stop(
    monkeypatch, tmp_path
) -> None:
    """A stop that *was* asked for is not a crash, so it ends the supervisor too."""
    import uvicorn

    from clear_record.web import app as web_app

    runs: list[int] = []

    def asked_to_stop(self) -> None:
        runs.append(1)
        self.should_exit = True  # what `POST /api/shutdown` sets

    monkeypatch.setattr(uvicorn.Server, "run", asked_to_stop)

    assert (
        web_app.serve(
            host="127.0.0.1",
            port=0,
            open_browser=False,
            data_dir=str(tmp_path),
            supervise=True,
        )
        == 0
    )
    assert runs == [1], "an asked stop is not restarted"
