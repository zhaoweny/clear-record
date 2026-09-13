"""Public entry point for the clear-record CLI.

The implementation lives in :mod:`cr_cli`; this package is the published
install name (`uvx clear-record`).
"""

from cr_cli.cli import main

__all__ = ["main"]
