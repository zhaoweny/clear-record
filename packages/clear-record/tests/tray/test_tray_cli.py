"""The `tray` subcommand registration and the missing-extra hint.

The Qt shell itself is not exercised here (no display in CI); the supervision
logic it wraps is covered in `test_service.py`.
"""

from __future__ import annotations

import pytest

from clear_record.cli import cli


def test_tray_subcommand_is_registered() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["tray", "--no-browser", "--port", "9001"])
    assert args.command == "tray"
    assert args.port == 9001
    assert callable(args.handler)


def test_missing_tray_extra_gives_an_actionable_hint(monkeypatch) -> None:
    import clear_record.tray as tray

    monkeypatch.setattr(tray, "_pyside6_available", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        tray._require_tray_stack()
    message = str(excinfo.value)
    assert "clear-record[tray]" in message
    assert "clear-record web" in message  # a way forward without Qt
