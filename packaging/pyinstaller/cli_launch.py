"""Frozen CLI entry point for the PyInstaller desktop build.

Identical to the installed `clear-record` console script, except that a
double-click (no arguments) prints help instead of erroring, so the bundled
binary is never a silent no-op.
"""

from __future__ import annotations

import multiprocessing
import sys

from clear_record.cli.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # Windows: safe under freeze
    argv = sys.argv[1:]
    raise SystemExit(main(argv if argv else ["--help"]))
