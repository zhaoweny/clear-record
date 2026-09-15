"""The `clear-record web` registration seam and the `web` extra hint.

The CLI discovers optional subcommands from the ``clear_record.commands``
entry-point group, so it never imports the web module (ADR-0013). The discovery
helper takes no arguments, so tests substitute the entry-point list directly.
Since ADR-0022 each provider contributes a Click command to the CLI's group.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from clear_record.cli import cli


class _FakeEntryPoint:
    name = "hello"

    def __init__(self, register):
        self._register = register

    def load(self):
        return self._register


def _parse(command: str, argv: list[str]) -> SimpleNamespace:
    cmd = cli._build_group().commands[command]
    with cmd.make_context(command, list(argv)) as ctx:
        return SimpleNamespace(**ctx.params)


def test_external_subcommand_is_registered_and_dispatched(monkeypatch) -> None:
    seen: dict = {}

    def register(group: click.Group) -> None:
        @group.command(name="hello", help="say hello")
        @click.option("--name", default="world")
        def hello(name: str) -> int:
            seen["name"] = name
            return 0

    monkeypatch.setattr(cli, "_external_commands", lambda: [_FakeEntryPoint(register)])
    group = cli._build_group()

    result = CliRunner().invoke(group, ["hello", "--name", "zhaow"])
    assert result.exit_code == 0
    assert seen == {"name": "zhaow"}


def test_no_external_commands_leaves_the_surface_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_external_commands", lambda: [])
    group = cli._build_group()
    assert "backends" in group.commands
    assert CliRunner().invoke(group, ["backends", "--all"]).exit_code == 0


def test_a_provider_that_cannot_import_is_skipped_not_fatal(monkeypatch) -> None:
    """A missing extra must not kill the CLI.

    Entry points are declared whether or not the extra that supplies their
    dependencies is installed, and the packaged macOS app shipped without
    clear_record.mcp, so an unguarded load() made the frozen CLI die on
    launch instead of exposing web and tray.
    """

    class _Unimportable:
        name = "mcp"

        def load(self):
            raise ImportError("No module named 'clear_record.mcp'")

    def register(group: click.Group) -> None:
        @group.command(name="hello", help="say hello")
        def hello() -> int:
            return 0

    monkeypatch.setattr(
        cli, "_external_commands", lambda: [_Unimportable(), _FakeEntryPoint(register)]
    )
    group = cli._build_group()
    assert "hello" in group.commands
    assert "backends" in group.commands


def test_web_entry_point_is_declared_in_the_installed_dist() -> None:
    declared = {
        ep.name: ep.value for ep in entry_points(group=cli.COMMAND_ENTRY_POINT_GROUP)
    }
    assert declared.get("web") == "clear_record.web:register"


def test_web_subcommand_is_registered_from_the_entry_point() -> None:
    assert "web" in cli._build_group().commands
    args = _parse("web", ["--no-browser", "--port", "9999"])
    assert args.port == 9999


def test_missing_web_extra_gives_an_actionable_hint(monkeypatch) -> None:
    import clear_record.web as web

    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(SystemExit) as excinfo:
        web._require_web_stack()
    message = str(excinfo.value)
    assert "clear-record[web]" in message
    assert "uvx" in message


def test_web_stack_is_present_in_the_dev_environment() -> None:
    import clear_record.web as web

    web._require_web_stack()  # must not raise when the dev deps are installed
