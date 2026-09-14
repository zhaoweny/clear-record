"""The `clear-record gui`/`web` registration seam and the `web` extra hint.

The CLI discovers optional subcommands from the ``clear_record.commands``
entry-point group, so it never imports the web module (ADR-0013). The discovery
helper takes no arguments, so tests substitute the entry-point list directly.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from clear_record.cli import cli


class _FakeEntryPoint:
    name = "hello"

    def __init__(self, register):
        self._register = register

    def load(self):
        return self._register


def test_external_subcommand_is_registered_and_dispatched(monkeypatch) -> None:
    seen: dict = {}

    def register(sub) -> None:
        parser = sub.add_parser("hello", help="say hello")
        parser.add_argument("--name", default="world")
        parser.set_defaults(handler=lambda args: seen.update(name=args.name) or 0)

    monkeypatch.setattr(cli, "_external_commands", lambda: [_FakeEntryPoint(register)])
    parser = cli._build_parser()

    args = parser.parse_args(["hello", "--name", "zhaow"])
    assert args.command == "hello"
    assert cli._main(args) == 0
    assert seen == {"name": "zhaow"}


def test_no_external_commands_leaves_the_surface_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_external_commands", lambda: [])
    parser = cli._build_parser()
    assert parser.parse_args(["backends", "--all"]).command == "backends"


def test_web_entry_point_is_declared_in_the_installed_dist() -> None:
    declared = {
        ep.name: ep.value for ep in entry_points(group=cli.COMMAND_ENTRY_POINT_GROUP)
    }
    assert declared.get("web") == "clear_record.web:register"


def test_web_subcommand_is_registered_from_the_entry_point() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["web", "--no-browser", "--port", "9999"])
    assert args.command == "web"
    assert args.port == 9999
    assert callable(args.handler)


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
