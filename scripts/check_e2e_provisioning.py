#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Provisioning guard for the browser end-to-end gate (``just e2e``).

``just e2e`` needs two things that live outside the Python environment and
that a freshly cut worktree does not have:

* the frontend's dependencies, installed by bun into
  ``packages/clear-record/frontend/node_modules``. Without them ``bun run e2e``
  dies at exit 127 with ``playwright: command not found``;
* Playwright's Chromium (``just e2e-install``). Without it every spec fails in
  ``browserType.launch`` — one identical error per test, burying the cause.

The ``e2e`` recipe runs this guard before the seed, the server boot and the
first spec, so the first piece that is missing is reported once — with the
command that provides it — instead of costing a diagnosis session.

The browser directory is ``$PLAYWRIGHT_BROWSERS_PATH``, the same value the
recipe hands Playwright, falling back to the repo's ``.local/ms-playwright``
when the script is run by hand. That download is per machine by convention, not
per worktree: it lands in the main checkout's ``.local/ms-playwright``, which a
worktree shares only when its own ``.local/ms-playwright`` is symlinked there.
See the ``e2e`` recipe comment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "packages" / "clear-record" / "frontend"
DEFAULT_BROWSERS_PATH = REPO_ROOT / ".local" / "ms-playwright"

# The one command that provisions the frontend's dependencies: the same `bun
# install` that `web-assets` runs first, repeated here so this guard and that
# recipe read alike.
BUN_INSTALL = "bun install --frozen-lockfile --cwd packages/clear-record/frontend"

# What Playwright launches for the `chromium` project: the full browser for
# headed runs and the headless shell it prefers by default. The install
# directory names carry a revision (`chromium-1243`, `chromium_headless_shell-
# 1243`), so match the executable, never the version — and let the `.app` name
# vary (`Chromium.app` before, `Google Chrome for Testing.app` since).
CHROMIUM_EXECUTABLES = (
    "chromium-*/chrome-mac*/*.app/Contents/MacOS/*",
    "chromium-*/chrome-linux*/chrome",
    "chromium-*/chrome-win*/chrome.exe",
    "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
    "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
)


def playwright_runner() -> Path:
    """The entry point ``bun run e2e`` executes, installed or not."""
    return FRONTEND / "node_modules" / ".bin" / "playwright"


def chromium_executables(browsers_path: Path) -> list[Path]:
    """Installed Chromium executables under ``browsers_path`` (empty if none)."""
    if not browsers_path.is_dir():
        return []
    return [
        found
        for pattern in CHROMIUM_EXECUTABLES
        for found in browsers_path.glob(pattern)
        if found.is_file()
    ]


def main() -> int:
    # The dependencies first: they are what a fresh worktree hits first, and
    # `bun run e2e` cannot even look for a browser until they are installed.
    runner = playwright_runner()
    if not runner.exists():
        print(
            "e2e: this worktree's frontend dependencies are not installed "
            f"(no `{runner.relative_to(REPO_ROOT)}`).\n"
            f"Install them per worktree with: `{BUN_INSTALL}`",
            file=sys.stderr,
        )
        return 2

    browsers_path = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or DEFAULT_BROWSERS_PATH
    )
    if chromium_executables(browsers_path):
        return 0

    print(
        f"e2e: no Playwright Chromium under {browsers_path}.\n"
        "Provision it with: just e2e-install\n"
        "(One per machine by convention, not per worktree: it lands in the main\n"
        " checkout's .local/ms-playwright, which a worktree shares only when its\n"
        " own .local/ms-playwright is symlinked there — provisioning does that,\n"
        " nothing committed here does.)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
