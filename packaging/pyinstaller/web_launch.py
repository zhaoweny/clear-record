"""Frozen web entry point: the double-click target of the desktop build.

Always starts the local console (`clear-record web`) and opens the browser.
Options are the `web` subcommand's, so ``clear-record-web --port 9000
--no-browser`` works; the separate `clear-record` binary is the full CLI.
"""

from __future__ import annotations

import multiprocessing
import sys

from clear_record.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows: safe under freeze
    raise SystemExit(main(["web", *sys.argv[1:]]))
