"""`clear-record diagnose` is registered by entry point, and `--verbose` raises
diagnostics detail directly (both before and after the subcommand).

A bare CLI `run` leaves log lines — the sink lives in ``core``, which the CLI may
import — and the default output is untouched: diagnostics go to the app state
directory, never to stdout.
"""

from __future__ import annotations

import json
import os
from importlib.metadata import entry_points
from types import SimpleNamespace

import pytest

from clear_record.cli import cli, stages
from clear_record.core import Step, diagnostics as core_diagnostics, read_recent


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    core_diagnostics.set_level(None)
    yield
    core_diagnostics.set_level(None)


def test_diagnose_entry_point_is_declared_in_the_dist() -> None:
    declared = {
        entry_point.name: entry_point.value
        for entry_point in entry_points(group=cli.COMMAND_ENTRY_POINT_GROUP)
    }
    assert declared.get("diagnose") == "clear_record.service.diagnostics:register"


def test_diagnose_subcommand_is_registered_from_the_entry_point() -> None:
    assert "diagnose" in cli._build_group().commands
    command = cli._build_group().commands["diagnose"]
    with command.make_context("diagnose", ["--list", "--include-private"]) as ctx:
        args = SimpleNamespace(**ctx.params)
    assert args.dry_run is True
    assert args.include_private is True


def test_verbose_raises_the_level_in_either_position(monkeypatch) -> None:
    monkeypatch.setenv("CR_LOG_LEVEL", "error")

    assert cli.main(["backends", "-v"]) == 0  # after the subcommand
    assert core_diagnostics.effective_level() == "debug"

    core_diagnostics.set_level(None)
    assert cli.main(["-v", "backends"]) == 0  # before the subcommand
    assert core_diagnostics.effective_level() == "debug"


def test_verbose_does_not_export_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("CR_LOG_LEVEL", raising=False)
    cli.main(["backends", "-v"])
    assert "CR_LOG_LEVEL" not in os.environ


def test_default_level_adds_nothing_to_stdout(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CR_LOG_LEVEL", raising=False)
    # A command that prints nothing, so any stdout would be a log line.
    monkeypatch.setattr(cli, "backend_availability", lambda: {})
    assert cli.main(["backends"]) == 0
    assert capsys.readouterr().out == ""
    assert core_diagnostics.effective_level() == "info"


def test_a_bare_cli_run_leaves_log_lines(tmp_path, monkeypatch) -> None:
    """The primary path: `clear-record run` writes a trail the bundle includes."""
    monkeypatch.setattr(
        stages, "_STAGE_RUNNERS", {step: (lambda *args: None) for step in Step}
    )
    stages.run(str(tmp_path))

    events = [json.loads(line)["event"] for line in read_recent(100)]
    assert "cli.run.started" in events
    assert "cli.run.finished" in events
    assert events.count("stage.started") == len(list(Step))
    assert events.count("stage.finished") == len(list(Step))

    # The bundle a user would send carries the same lines.
    from clear_record.service import collect_bundle

    text = collect_bundle(limit=100)
    assert "cli.run.started" in text
    assert "cli.run.finished" in text
