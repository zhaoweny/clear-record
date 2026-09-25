"""The tray supervisor: a native entry point for the whole console.

`clear-record tray` serves the local console under a **system-tray icon** for a
menu bar, with the console's node where it belongs: a node that is already
listening is **joined** rather than started a second time, and one is started (in
this process) only when nothing answers. The icon's jobs are to open the console,
show whether its node is answering, restart a node this tray started, and quit
cleanly.

Importing this package is **light** (like :mod:`clear_record.web`): the CLI
discovers the ``tray`` subcommand through an entry point while building the
parser, so Qt is imported only when the command actually runs.

The Qt binding is **PySide6**. PySide2 is not an option: its wheels stop at
Python 3.10 and this project requires >=3.12.
"""

from __future__ import annotations

import importlib.util

import click

from clear_record.core.node import DEFAULT_HOST, DEFAULT_PORT

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
        name="tray",
        help="serve the console behind a system-tray icon, joining a node",
    )
    @click.option(
        "--host",
        default=DEFAULT_HOST,
        help="bind address for a node this tray starts (default localhost)",
    )
    @click.option(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            f"port for a node this tray starts (default {DEFAULT_PORT}); "
            "a node already listening is joined instead"
        ),
    )
    @click.option("--no-browser", is_flag=True, help="do not open a browser on start")
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help=(
            "data directory for a node this tray starts "
            "(default: CR_DATA_DIR / platform dir); a node already listening "
            "is joined instead"
        ),
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


__all__ = ["register"]
