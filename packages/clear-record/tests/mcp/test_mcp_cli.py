"""The `mcp` subcommand registration and the missing-extra hint.

The MCP SDK itself is exercised through the in-process client in
``test_mcp_tools.py``; here we only cover the CLI seam (ADR-0013): the subcommand
is registered from the entry point and the ``agents`` extra's absence is an
actionable message, not an import traceback.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from clear_record.cli import cli


def test_mcp_subcommand_is_registered() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["mcp", "--data-dir", "/tmp/cr"])
    assert args.command == "mcp"
    assert args.data_dir == "/tmp/cr"
    assert callable(args.handler)


def test_missing_mcp_extra_gives_an_actionable_hint(monkeypatch) -> None:
    import clear_record.mcp as mcp

    monkeypatch.setattr(mcp.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(SystemExit) as excinfo:
        mcp._require_mcp_stack()
    message = str(excinfo.value)
    assert "clear-record[agents]" in message
    assert "uvx" in message
    assert "clear-record web" in message  # a way forward with no agent


def test_mcp_stack_is_present_in_the_dev_environment() -> None:
    import clear_record.mcp as mcp

    mcp._require_mcp_stack()  # must not raise when the dev deps are installed


def test_mcp_entry_point_is_declared_in_the_installed_dist() -> None:
    declared = {
        ep.name: ep.value for ep in entry_points(group=cli.COMMAND_ENTRY_POINT_GROUP)
    }
    assert declared.get("mcp") == "clear_record.mcp:register"
