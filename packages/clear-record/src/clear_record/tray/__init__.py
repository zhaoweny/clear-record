"""The tray supervisor: a native entry point for the whole console.

`clear-record tray` runs the local console as a managed background service and
puts a **system-tray icon** in the menu bar for supervising it: open the
console, see where it is listening, and quit cleanly.

Importing this package is **light** (like :mod:`clear_record.web`): the CLI
discovers the ``tray`` subcommand through an entry point while building the
parser, so Qt is imported only when the command actually runs.

The Qt binding is **PySide6**. PySide2 is not an option: its wheels stop at
Python 3.10 and this project requires >=3.12.
"""

from __future__ import annotations

import importlib.util

import click

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_TRAY_STACK = ("PySide6",)

_MISSING_TRAY_HINT = (
    "[tray] the native tray app needs the optional 'tray' extra (PySide6 not found).\n"
    "  Install it with:   pip install 'clear-record[tray]'\n"
    "  Or run without installing:   uvx --from 'clear-record[tray]' clear-record tray\n"
    "  Prefer no desktop app?   clear-record web"
)


def _pyside6_available() -> bool:
    return importlib.util.find_spec("PySide6") is not None


def _require_tray_stack() -> None:
    """Fail with an actionable hint when the `tray` extra is not installed."""
    from clear_record.web import _require_web_stack

    _require_web_stack()  # the tray supervises the web console
    if not _pyside6_available():
        raise SystemExit(_MISSING_TRAY_HINT)


def register(group: click.Group) -> None:
    """Add the ``tray`` subcommand (called by the CLI's entry-point discovery)."""

    @group.command(
        name="tray", help="run the console in the background with a system-tray icon"
    )
    @click.option(
        "--host", default=DEFAULT_HOST, help="bind address (default localhost)"
    )
    @click.option("--port", type=int, default=DEFAULT_PORT, help="port (default 8765)")
    @click.option("--no-browser", is_flag=True, help="do not open a browser on start")
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help="override the app data directory (default: CR_DATA_DIR / XDG)",
    )
    def _tray(host: str, port: int, no_browser: bool, data_dir: str | None) -> int:
        return _run(host=host, port=port, no_browser=no_browser, data_dir=data_dir)


def _run(
    *,
    host: str,
    port: int,
    no_browser: bool,
    data_dir: str | None,
) -> int:
    _require_tray_stack()
    from clear_record.tray.app import main as tray_main

    return tray_main(
        host=host,
        port=port,
        data_dir=data_dir,
        open_browser=not no_browser,
    )


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "register"]
