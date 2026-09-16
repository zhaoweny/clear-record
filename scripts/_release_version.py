#!/usr/bin/env python3
"""Shared release-train plumbing for the ``bump-*`` pointer scripts.

``bump_dev.py`` and ``bump_rc.py`` each own one transition uv cannot express,
but they read and write the version the same way: both literals move -- the
member (the published dist) and the vestigial root, kept in step -- through
``uv version --no-sync``, exactly like the other bump recipes; ``uv.lock``
records the version, so ``uv version`` relocks as it writes.

This module is the one owner of that read/write pair and of the version shapes
both scripts split on. It is imported, never run directly: the entry points stay
`just bump-dev` / `just bump-rc`, and `uv run --no-project` puts this script's
directory on `sys.path` so the sibling import resolves.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: `X.Y.Z` followed by whatever suffix the train carries.
VERSION = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<suffix>.*)$")
DEV = re.compile(r"^\.dev(?P<n>\d+)$")
RC = re.compile(r"^rc(?P<n>\d+)(?P<rest>\.dev\d+)?$")


def env() -> dict[str, str]:
    """The subprocess environment, without the caller's VIRTUAL_ENV.

    The recipe runs a bump script through `uv run --no-project`, which exports
    VIRTUAL_ENV pointing at its ephemeral environment; that makes the inner
    `uv version` warn that it does not match the project's `.venv`.
    """
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


def current_version() -> str:
    """The member version the tooling reads."""
    result = subprocess.run(
        ["uv", "version", "--frozen", "--package", "clear-record", "--short"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env(),
    )
    return result.stdout.strip()


def set_version(target: str) -> None:
    """Move both version literals (the member and the virtual root) to `target`."""
    for argv in (
        ["uv", "version", "--no-sync", "--package", "clear-record", target],
        ["uv", "version", "--no-sync", target],
    ):
        subprocess.run(argv, cwd=REPO_ROOT, check=True, env=env())


def run_bump(label: str, version: str, target: str) -> int:
    """Print `label: old -> new`, write it, and return the process exit code."""
    print(f"{label}: {version} -> {target}")
    set_version(target)
    return 0
