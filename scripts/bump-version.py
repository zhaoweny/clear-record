#!/usr/bin/env python3
"""Set the clear-record workspace version across the root and every member.

The virtual root and the workspace members must always declare the same
``version`` and exact-pin one another to it (ADR-0009, ADR-0011). ``uv version
--bump dev`` bumps one project and leaves the sibling ``==`` pins alone, so a
workspace bump needs this script: it owns the version literal for every
manifest and rewrites every occurrence in one move.

The members are discovered by glob (``packages/*/pyproject.toml``), so adding a
workspace member needs no edit here. Stdlib only (``re``, ``pathlib``, ``sys``),
so plain ``python3 scripts/bump-version.py`` works too (the publish-job guard
calls it that way).
Usage::

    uv run scripts/bump-version.py --show      # print, aborting if they differ
    uv run scripts/bump-version.py --dev       # 0.1.1 -> 0.1.1.dev0; .devN -> .devN+1
    uv run scripts/bump-version.py 0.1.1       # explicit (a release drops the suffix)

The branchy part lives here; the ``just`` recipes are thin pointers.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The virtual root plus every workspace member, discovered by glob so a new
# member needs no edit here. The root is not under `packages/`, hence explicit.
MANIFESTS = [
    REPO_ROOT / "pyproject.toml",
    *sorted(REPO_ROOT.glob("packages/*/pyproject.toml")),
]

# PEP 440 for the two shapes this project publishes: X.Y.Z or X.Y.Z.devN.
# Local versions (`+g<sha>`) are deliberately absent — PyPI rejects them.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:\.dev\d+)?")
_VERSION_LINE_RE = re.compile(r'^version = "([^"]+)"$', re.MULTILINE)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_versions() -> list[str]:
    versions: list[str] = []
    for path in MANIFESTS:
        match = _VERSION_LINE_RE.search(path.read_text())
        if match is None:
            fail(f'no `version = "..."` line in {path.relative_to(REPO_ROOT)}')
        versions.append(match.group(1))
    return versions


def current_version() -> str:
    """Return the shared version, or fail if the manifests disagree."""
    versions = read_versions()
    if len(set(versions)) != 1:
        details = ", ".join(
            f"{path.relative_to(REPO_ROOT)}={version}"
            for path, version in zip(MANIFESTS, versions)
        )
        fail(f"manifests are not in lockstep: {details}")
    return versions[0]


def next_dev(version: str) -> str:
    base, _, dev = version.partition(".dev")
    return f"{base}.dev0" if not dev else f"{base}.dev{int(dev) + 1}"


def rewrite(old: str, new: str) -> None:
    """Replace the exact old version literal in every manifest.

    Replacing the literal (not parsing TOML) keeps formatting intact and cannot
    miss a pin: at this point every file names exactly one version.
    """
    for path in MANIFESTS:
        path.write_text(path.read_text().replace(old, new))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        fail("usage: bump-version.py {--show|--dev|X.Y.Z[.devN]}")

    old = current_version()
    arg = argv[0]

    if arg == "--show":
        print(old)
        return 0

    if arg == "--dev":
        new = next_dev(old)
    elif _VERSION_RE.fullmatch(arg):
        new = arg
    else:
        fail(f"not a PEP 440 X.Y.Z[.devN] version: {arg!r}")

    if new == old:
        print(f"{old} => {new} (unchanged)")
        return 0

    rewrite(old, new)
    print(f"{old} => {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
