#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Advance the dev snapshot on `main` past a release candidate.

`uv version --bump dev` advances a `.devN` version but refuses to touch an
`rc`: the bump would not increase the version, so uv rejects it. That leaves
`main` stuck on the candidate it just tagged, and a wheel built from `main`
then carries the *tagged* version. This script owns the one transition uv
cannot express:

    X.Y.Z.devN     -> X.Y.Z.dev(N+1)       the ordinary next dev snapshot
    X.Y.ZrcN.devM  -> X.Y.ZrcN.dev(M+1)    another snapshot before the cut
    X.Y.ZrcN       -> X.Y.Zrc(N+1).dev0    open the next candidate

A stable `X.Y.Z` is refused with the explicit form to use instead, because
`uv version --bump` refuses that too. Both literals move -- the member (the
published dist) and the vestigial root, kept in step -- through
`uv version --no-sync`, exactly like the other bump recipes; `uv.lock` records
the version, so `uv version` relocks as it writes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: `X.Y.Z` followed by whatever suffix the train carries.
_VERSION = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<suffix>.*)$")
_DEV = re.compile(r"^\.dev(?P<n>\d+)$")
_RC = re.compile(r"^rc(?P<n>\d+)(?P<rest>\.dev\d+)?$")


def _env() -> dict[str, str]:
    """The subprocess environment, without the caller's VIRTUAL_ENV.

    The recipe runs this script through `uv run --no-project`, which exports
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
        env=_env(),
    )
    return result.stdout.strip()


def next_version(version: str) -> str:
    """The next dev snapshot, or exit with the rule the caller should use."""
    match = _VERSION.match(version)
    if match is None:
        raise SystemExit(f"bump-dev: cannot read a version out of {version!r}")
    base, suffix = match.group("base"), match.group("suffix")
    dev = _DEV.match(suffix)
    if dev is not None:
        return f"{base}.dev{int(dev.group('n')) + 1}"
    rc = _RC.match(suffix)
    if rc is not None:
        rest = rc.group("rest")
        if rest is not None:
            return f"{base}rc{int(rc.group('n'))}.dev{int(rest[4:]) + 1}"
        return f"{base}rc{int(rc.group('n')) + 1}.dev0"
    raise SystemExit(
        f"bump-dev: {version} is not a dev or rc version; start the next "
        f"dev series with `just set-version {base}.dev0`"
    )


def main() -> int:
    version = current_version()
    target = next_version(version)
    print(f"bump-dev: {version} -> {target}")
    for argv in (
        ["uv", "version", "--no-sync", "--package", "clear-record", target],
        ["uv", "version", "--no-sync", target],
    ):
        subprocess.run(argv, cwd=REPO_ROOT, check=True, env=_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
