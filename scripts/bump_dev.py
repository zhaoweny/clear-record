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
`uv version --bump` refuses that too. The read/write pair and the version
shapes live in the shared `_release_version` helper.
"""

from __future__ import annotations

import sys

from _release_version import DEV, RC, VERSION, current_version, run_bump


def next_version(version: str) -> str:
    """The next dev snapshot, or exit with the rule the caller should use."""
    match = VERSION.match(version)
    if match is None:
        raise SystemExit(f"bump-dev: cannot read a version out of {version!r}")
    base, suffix = match.group("base"), match.group("suffix")
    dev = DEV.match(suffix)
    if dev is not None:
        return f"{base}.dev{int(dev.group('n')) + 1}"
    rc = RC.match(suffix)
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
    return run_bump("bump-dev", version, next_version(version))


if __name__ == "__main__":
    sys.exit(main())
