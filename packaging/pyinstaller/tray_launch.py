"""Frozen tray entry point: the double-click target of the desktop build.

Always starts the native tray (`clear-record tray`) — the console as a managed
background service behind a menu-bar icon, joining a node already up. Options
are the `tray` subcommand's, so ``clear-record-tray --port 9000 --no-browser``
works; the separate `clear-record-web` binary starts the console without a tray,
and `clear-record` is the full CLI.
"""

from __future__ import annotations

import multiprocessing
import sys

from clear_record.cli.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows: safe under freeze
    raise SystemExit(main(["tray", *sys.argv[1:]]))
