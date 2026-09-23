#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Freshness guard for the console's committed front-end build output.

Rebuilds the assets under ``packages/clear-record/frontend/`` and fails if what
is **recorded** for ``clear_record/web/static/`` no longer matches their source —
the same discipline ``uv.lock`` gets from ``--locked`` under ``just verify``.

What "recorded" means here is the **index**, not ``HEAD``: the question is
whether a fresh build reproduces the files this tree is about to record, so the
comparison is the worktree against the index (``git diff``), plus the files the
build left under ``static/`` that are untracked **and not ignored**
(``git ls-files --others --exclude-standard``). What the ignore rules cover is not
this guard's business: a build artifact that ``.gitignore`` excludes is on disk by
design and unrecorded by design, so it neither fails the guard nor is promised to.
``git status --porcelain`` was wrong for that question — it reports a *staged*
difference as stale too, which is exactly the state a squash-merge leaves behind
(the merge stages its own build output),
so the guard failed on a tree where a fresh build reproduced the staged bytes
exactly. On a clean checkout and in CI the two questions coincide.

The guard needs ``bun``, so it is deliberately **not** part of ``just verify``: a
contributor who never touches the console must be able to run the Python gate
without Node. CI runs it as its own job, and ``just web-assets-check`` runs it
locally. See ADR-0023 and docs/frontend-assets.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "packages" / "clear-record" / "frontend"
STATIC = (
    REPO_ROOT / "packages" / "clear-record" / "src" / "clear_record" / "web" / "static"
)


def main() -> int:
    if shutil.which("bun") is None:
        print(
            "web-assets-check: `bun` is not on PATH.\n"
            "Install bun (https://bun.sh) to change the console's UI; "
            "`just verify` does not need it.",
            file=sys.stderr,
        )
        return 2

    subprocess.run(
        ["bun", "install", "--frozen-lockfile"],
        cwd=FRONTEND,
        check=True,
    )
    subprocess.run(["bun", "run", "build"], cwd=FRONTEND, check=True)

    # The fresh build is in the worktree; what is recorded is the index. So the
    # check is "did the build change a recorded file" plus "did it leave a file
    # nothing records" — never "does the worktree differ from HEAD", which a
    # staged build of the current source legitimately does.
    def recorded_differences(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args, "--", str(STATIC)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    changed = recorded_differences(["diff", "--name-only"])
    untracked = recorded_differences(["ls-files", "--others", "--exclude-standard"])
    stale = "\n".join(part for part in (changed, untracked) if part)
    if stale:
        print(
            "web-assets-check: the committed console assets are stale — the "
            "build output differs from its source:\n\n"
            f"{stale}\n\n"
            "Run `just web-assets` and commit the result.",
            file=sys.stderr,
        )
        return 1

    print("web-assets-check: OK — committed assets match the front-end source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
