"""The bundled web surface for clear-record.

Importing this package is **deliberately light**: it must not import FastAPI or
uvicorn, because the CLI discovers the ``web`` subcommand through an entry point
while building the parser — and a plain `clear-record ingest` should not pay the
web stack's import cost. The app is imported inside the handler.

The subcommand is registered through the ``clear_record.commands`` entry-point
group (ADR-0013) so ``clear_record.cli`` never statically imports this module.
"""

from __future__ import annotations

import importlib.util

import click

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Everything the console needs at run time. `clear_record.web` itself is always
#: importable (it ships in the one wheel); these are the optional `web` extra.
_WEB_STACK = ("fastapi", "uvicorn", "jinja2", "python_multipart")

_MISSING_EXTRA_HINT = (
    "[web] the web console needs the optional 'web' extra ({missing} not found).\n"
    "  Install it with:   pip install 'clear-record[web]'\n"
    "  Or run without installing:   uvx --from 'clear-record[web]' clear-record web"
)


def _require_web_stack() -> None:
    """Fail with an actionable hint when the `web` extra is not installed."""
    missing = [name for name in _WEB_STACK if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(_MISSING_EXTRA_HINT.format(missing=", ".join(missing)))


def register(group: click.Group) -> None:
    """Add the ``web`` subcommand (called by the CLI's entry-point discovery).

    ``group`` is the CLI's Click group (ADR-0022). Registered unconditionally:
    the subcommand is visible in ``--help`` even without the extra, and running
    it explains how to install the extra.
    """

    @group.command(
        name="web",
        help="start the local web console (projects, glossary) in a browser",
    )
    @click.option(
        "--host", default=DEFAULT_HOST, help="bind address (default localhost)"
    )
    @click.option("--port", type=int, default=DEFAULT_PORT, help="port (default 8765)")
    @click.option("--no-browser", is_flag=True, help="do not open a browser window")
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help="override the app data directory (default: CR_DATA_DIR / XDG)",
    )
    def _web(host: str, port: int, no_browser: bool, data_dir: str | None) -> int:
        return _run(host=host, port=port, no_browser=no_browser, data_dir=data_dir)


def _run(
    *,
    host: str,
    port: int,
    no_browser: bool,
    data_dir: str | None,
) -> int:
    _require_web_stack()
    from clear_record.web.app import serve

    return serve(
        host=host,
        port=port,
        open_browser=not no_browser,
        data_dir=data_dir,
    )


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "register"]
