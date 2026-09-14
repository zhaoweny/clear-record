#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Freshness guard for the console's committed front-end build output.

Rebuilds the assets under ``packages/clear-record/frontend/`` and fails if the
committed files in ``clear_record/web/static/`` no longer match their source —
the same discipline ``uv.lock`` gets from ``--locked`` under ``just verify``.

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

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", str(STATIC)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        print(
            "web-assets-check: the committed console assets are stale — the "
            "build output differs from its source:\n\n"
            f"{dirty}\n\n"
            "Run `just web-assets` and commit the result.",
            file=sys.stderr,
        )
        return 1

    print("web-assets-check: OK — committed assets match the front-end source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
