"""The `tray` subcommand registration and the missing-extra hint.

The Qt shell itself is not exercised here (no display in CI); the supervision
logic it wraps is covered in `test_service.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clear_record.cli import cli


def _parse(command: str, argv: list[str]) -> SimpleNamespace:
    cmd = cli._build_group().commands[command]
    with cmd.make_context(command, list(argv)) as ctx:
        return SimpleNamespace(**ctx.params)


def test_tray_subcommand_is_registered() -> None:
    assert "tray" in cli._build_group().commands
    args = _parse("tray", ["--no-browser", "--port", "9001"])
    assert args.port == 9001


def test_missing_tray_extra_gives_an_actionable_hint(monkeypatch) -> None:
    import clear_record.tray as tray

    monkeypatch.setattr(tray, "_pyside6_available", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        tray._require_tray_stack()
    message = str(excinfo.value)
    assert "clear-record[tray]" in message
    assert "clear-record web" in message  # a way forward without Qt
