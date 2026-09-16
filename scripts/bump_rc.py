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

A stable `X.Y.Z` is refused, as uv refuses it: use `just set-version X.Y.Z`.
The read/write pair and the version shapes live in the shared
`_release_version` helper.
"""

from __future__ import annotations

import sys

from _release_version import DEV, RC, VERSION, current_version, run_bump


def next_version(version: str) -> str:
    """The version to cut, or exit with the rule the caller should use."""
    match = VERSION.match(version)
    if match is None:
        raise SystemExit(f"bump-rc: cannot read a version out of {version!r}")
    base, suffix = match.group("base"), match.group("suffix")
    rc = RC.match(suffix)
    if rc is not None:
        if DEV.match(rc.group("rest") or ""):
            return f"{base}rc{int(rc.group('n'))}"
        return f"{base}rc{int(rc.group('n')) + 1}"
    if DEV.match(suffix):
        return f"{base}rc1"
    raise SystemExit(
        f"bump-rc: {version} is not a dev or rc version; cut the first rc with "
        f"`just set-version {base}rc1`"
    )


def main() -> int:
    version = current_version()
    return run_bump("bump-rc", version, next_version(version))


if __name__ == "__main__":
    sys.exit(main())
