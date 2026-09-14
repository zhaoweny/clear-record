"""Guard: committed files never reference the unpublished local tracker.

The issue tracker lives in ``.scratch/``, which is gitignored and never
published — ``docs/agents/issue-tracker.md`` defines it, and it sits on the same
environment-local boundary as recordings and model weights (ADR-0006). A
committed file therefore must not reference it **by path**: a reader of the
public repo can follow neither a markdown link nor a backticked path into a
directory that is not there, and nothing in a normal checkout flags the rot.

The rule (``docs/agents/issue-tracker.md`` § Citing the tracker from committed
files):

- **Never link** into the tracker.
- **Never cite a tracker file as evidence.** Durable claims stand on durable
  sources: an ADR, ``docs/research/``, or ``docs/vox/voice-of-owner.md``.
- **Naming a lane in prose is fine** ("the ``hardware-backends`` lane of the
  local tracker"). The banned thing is the path, not the concept.
- **The arrow points one way.** Tracker entries link *to* ``docs/``; a
  committed file never links back into the tracker. A tracker finding that
  needs to be citable graduates into ``docs/``.

Two deliberate scoping choices:

- Every file git would publish is checked — **tracked files, plus untracked
  files that are not ignored** — so a freshly written document is caught before
  it is ever staged, and `just verify` agrees with CI.
- Only four files are exempt, because they are the ones that define or explain
  the boundary itself.

The scan **fails loudly when it cannot enumerate tracked files**. A guard that
silently passes when it cannot see the tree is worse than no guard at all.

This is the ``test_layering.py`` pattern: a repo-structure invariant enforced by
the suite, so ``just verify`` and CI check it with no extra job or recipe.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Gitignored path roots a committed file must not reference.
#
# Each entry is a *repo-relative path root*, matched as a path segment rather
# than as a bare substring: ``.scratch`` matches ``.scratch/…``, a bare
# ``.scratch``, and ``../.scratch/…`` (a relative link), but not ``.scratchpad``
# (a longer name). Home-relative and absolute paths are excluded separately, so
# that a ``.local`` entry cannot trip on ``~/.local/share/…`` or ``/home/…``.
#
# v1 covers the local issue tracker only. Extending the guard to another
# gitignored class (``workspace/``, ``.local/``, ``.wt/`` …) is one entry here,
# plus a ruling on which files may legitimately name it.
IGNORED_PATH_ROOTS = (".scratch",)

# Files exempt because they define or explain the boundary: the ignore rule
# itself, the standing instructions, the tracker convention, and this guard
# (whose docstring must name the directory to explain what it bans).
EXEMPT = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "docs/agents/issue-tracker.md",
        "packages/clear-record/tests/test_tracker_refs.py",
    }
)

# The no-op guard below: a sanity floor for the file count, plus files every
# checkout has. A renamed or missing one means the enumeration is broken.
MIN_TRACKED_FILES = 100
KNOWN_FILES = ("AGENTS.md", "pyproject.toml", "justfile")


def _tracked_files() -> list[str]:
    """Every file git would publish, repo-relative, or fail loudly.

    ``--cached --others --exclude-standard`` is tracked files plus untracked
    files that are not ignored: exactly the set a commit would carry. A plain
    ``--cached`` would miss a document written but not yet staged, so the guard
    would pass locally and fail in CI.

    Raises ``AssertionError`` rather than returning an empty list when ``git``
    is unavailable or errors: an empty scan must never read as a clean scan.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - no git binary
        raise AssertionError(
            f"cannot enumerate tracked files: {exc}. This guard checks *published* "
            "files, so it cannot run without a work tree."
        ) from exc
    if result.returncode != 0:
        raise AssertionError(
            "cannot enumerate published files: "
            f"`git ls-files` exited {result.returncode}: {result.stderr.strip()}"
        )
    return [name for name in result.stdout.split("\0") if name]


# Characters that may appear inside a path token, used to look back from a
# candidate root to the start of the path it sits in.
_PATH_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/~-"

# Each root matched as a *path segment*: bound to a segment boundary so it is
# not glued to a longer name (``.scratchpad``).
_SEGMENT = {
    root: re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(root)}(?![A-Za-z0-9_-])")
    for root in IGNORED_PATH_ROOTS
}


# A leading run of relative-link prefixes, stripped before judging whether a
# path is repo-relative: `../.scratch/…` from `docs/research/` is repo-relative.
_REL_PREFIX = re.compile(r"^(?:\.\.?/)+")


def _references_root(line: str, root: str) -> bool:
    """True when *line* uses *root* as a repo-relative path segment.

    A bare substring test is wrong in both directions: it flags ``.scratchpad``,
    and it would flag ``~/.local/share/…`` the moment ``.local`` joined the set.
    So a candidate must be bounded like a path segment, and the path it sits in
    must be *repo-relative* once any ``./``/``../`` lead-in is stripped — not
    home-relative (``~/.local``) and not absolute (``/home/…/.local``).
    ``../.scratch/…`` therefore counts: the three dead links this guard was
    written for were exactly that shape.
    """
    for match in _SEGMENT[root].finditer(line):
        start = len(line[: match.start()].rstrip(_PATH_CHARS))
        path = _REL_PREFIX.sub("", line[start : match.start()])
        if not path.startswith(("~", "/")):
            return True
    return False


def _offences(name: str, text: str) -> list[str]:
    """Lines of *name* that reference an ignored path root."""
    return [
        f"{name}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if any(_references_root(line, root) for root in IGNORED_PATH_ROOTS)
    ]


def test_the_scan_actually_scans() -> None:
    """Guard against a silent no-op scan (a broken enumeration reads as clean)."""
    tracked = _tracked_files()
    assert len(tracked) >= MIN_TRACKED_FILES, (
        f"only {len(tracked)} tracked files found; the enumeration is probably "
        "broken, and a broken guard must not pass."
    )
    missing = sorted(
        name for name in {*EXEMPT, *KNOWN_FILES} if not (REPO_ROOT / name).is_file()
    )
    assert not missing, f"file(s) the guard relies on no longer exist: {missing}"


def test_committed_files_do_not_reference_the_tracker() -> None:
    """No committed file outside the exempt set references an ignored path root.

    Durable documents cite durable sources. If a tracker entry carries a finding
    worth citing, it graduates into ``docs/`` and *that* is what gets linked.
    """
    violations: list[str] = []
    for name in _tracked_files():
        if name in EXEMPT:
            continue
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary: it cannot carry a followable reference.
            continue
        violations.extend(_offences(name, text))
    assert not violations, (
        "committed files must not reference the unpublished local tracker "
        f"({', '.join(IGNORED_PATH_ROOTS)}). Name the lane in prose, cite a durable "
        "source, or graduate the finding into docs/. See docs/agents/"
        "issue-tracker.md § Citing the tracker from committed files.\n"
        + "\n".join(violations)
    )
