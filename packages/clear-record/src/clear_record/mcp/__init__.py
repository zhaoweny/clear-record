"""The MCP server: the agent boundary for clear-record.

``clear-record mcp`` starts a **stdio** Model Context Protocol server that exposes
the application service as semantic tools, so a user's own MCP-capable agent can
drive the whole workflow (projects, glossary, meetings, runs, artifacts) without
the GUI. It is a **thin adapter**: every tool is a small translation of a
``clear_record.service`` call, and no domain logic lives here.

Importing this package is **light** (like :mod:`clear_record.web` and
:mod:`clear_record.tray`): the CLI discovers the ``mcp`` subcommand through the
``clear_record.commands`` entry point while building the parser, so the MCP SDK
is imported only when the command actually runs.

**BYOK.** The server never reads, requires or bundles a model or provider
credential: the user's agent brings its own. The harness stays outside
``clear_record.core`` (ADR-0017, following maa-whirlwind ADR-0005).
"""

from __future__ import annotations

import argparse
import importlib.util
from typing import Any

#: Everything the MCP server needs at run time. `clear_record.mcp` itself is
#: always importable (it ships in the one wheel); this is the optional `agents`
#: extra.
_MCP_STACK = ("mcp",)

_MISSING_EXTRA_HINT = (
    "[mcp] the MCP agent server needs the optional 'agents' extra ({missing} not found).\n"
    "  Install it with:   pip install 'clear-record[agents]'\n"
    "  Or run without installing:   uvx --from 'clear-record[agents]' clear-record mcp\n"
    "  No agent?   clear-record web"
)


def _require_mcp_stack() -> None:
    """Fail with an actionable hint when the `agents` extra is not installed."""
    missing = [name for name in _MCP_STACK if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit(_MISSING_EXTRA_HINT.format(missing=", ".join(missing)))


def register(subparsers: Any) -> None:
    """Add the ``mcp`` subcommand (called by the CLI's entry-point discovery).

    Registered unconditionally: the subcommand is visible in ``--help`` even
    without the extra, and running it explains how to install the extra.
    """
    parser = subparsers.add_parser(
        "mcp",
        help="run the MCP server over the service (for your own AI agent)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="override the app data directory (default: CR_DATA_DIR / XDG)",
    )
    parser.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    _require_mcp_stack()
    from clear_record.mcp.server import main as mcp_main

    return mcp_main(data_dir=args.data_dir)


__all__ = ["register"]
