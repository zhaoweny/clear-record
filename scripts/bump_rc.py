#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cut or advance the release-candidate segment.

`uv version --bump rc` gets one of the train's three cases wrong. The model is
that a `.devN` suffix on `rcN` is a snapshot *of* that candidate, so cutting it
drops the suffix:

    X.Y.Z.devN     -> X.Y.Zrc1       uv: correct (the first candidate)
    X.Y.ZrcN       -> X.Y.Zrc(N+1)   uv: correct (advance after one is cut)
    X.Y.ZrcN.devM  -> X.Y.ZrcN       uv gives rc(N+1); this script drops .devM

A stable X.Y.Z is refused, as uv refuses it: use `just set-version X.Y.Z`.

Both literals move (member + root) through `uv version --no-sync`, as the
sibling bump scripts do.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_VERSION = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?P<suffix>.*)$")
_DEV = re.compile(r"^\.dev\d+$")
_RC = re.compile(r"^rc(?P<n>\d+)(?P<rest>\.dev\d+)?$")


def _env() -> dict[str, str]:
    """The subprocess environment, without the caller's VIRTUAL_ENV."""
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
    """The version to cut, or exit with the rule the caller should use."""
    match = _VERSION.match(version)
    if match is None:
        raise SystemExit(f"bump-rc: cannot read a version out of {version!r}")
    base, suffix = match.group("base"), match.group("suffix")
    rc = _RC.match(suffix)
    if rc is not None:
        if _DEV.match(rc.group("rest") or ""):
            return f"{base}rc{int(rc.group('n'))}"
        return f"{base}rc{int(rc.group('n')) + 1}"
    if _DEV.match(suffix):
        return f"{base}rc1"
    raise SystemExit(
        f"bump-rc: {version} is not a dev or rc version; cut the first rc with "
        f"`just set-version {base}rc1`"
    )


def main() -> int:
    version = current_version()
    target = next_version(version)
    print(f"bump-rc: {version} -> {target}")
    for argv in (
        ["uv", "version", "--no-sync", "--package", "clear-record", target],
        ["uv", "version", "--no-sync", target],
    ):
        subprocess.run(argv, cwd=REPO_ROOT, check=True, env=_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())
