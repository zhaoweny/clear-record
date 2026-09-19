"""Guard: committed files never reference the unpublished tracker.

The tracker moved to the owner's private Gitea instance, reachable only over the
tailnet (ADR-0029); the pre-migration corpus stays in ``.scratch/`` as a frozen
archive, gitignored and never published — ``docs/agents/issue-tracker.md``
defines the boundary, which is the same environment-local one as recordings and
model weights (ADR-0006). A committed file therefore must not reference the
tracker **by path or hostname**: a reader of the public repo can follow neither
a markdown link nor a backticked path into a directory that is not there, nor a
ticket URL into a host they cannot reach, and nothing in a normal checkout flags
the rot.

The rule (``docs/agents/issue-tracker.md`` § Citing the tracker from committed
files):

- **Never link** into the tracker or the instance.
- **Never cite a ticket as evidence.** Durable claims stand on durable sources:
  an ADR, ``docs/research/``, or ``docs/vox/voice-of-owner.md``.
- **Naming a lane in prose is fine** ("the ``hardware-backends`` lane of the
  tracker"). The banned thing is the address, not the concept.
- **The arrow points one way.** Tracker entries link *to* ``docs/``; a
  committed file never links back into the tracker. A tracker finding that
  needs to be citable graduates into ``docs/``.

Two deliberate scoping choices:

- Every file git would publish is checked — **tracked files, plus untracked
  files that are not ignored** — so a freshly written document is caught before
  it is ever staged, and `just verify` agrees with CI.
- Only five files are exempt, because they are the ones that define or explain
  the boundary itself. This guard's own membership is the rule's **one recorded
  exception**: a ban has to write down the name it bans, so the hostname literal
  below is the one committed place it may stand. The owner accepted it standing
  when it was raised, on 2026-09-19 — recorded beside the rule it excepts in
  ``docs/agents/issue-tracker.md``; any *other* file naming it is a finding, not
  a second exception.

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
# ``.scratch`` holds the tracker's frozen pre-migration archive (ADR-0029). The
# live tracker is a hostname rather than a path, so it is matched by
# ``PRIVATE_HOSTS`` below. Extending either set to another private class
# (``workspace/``, ``.local/``, ``.wt/`` …) is one entry, plus a ruling on which
# files may legitimately name it.
IGNORED_PATH_ROOTS = (".scratch",)

# Hostnames of private services a committed file must not name: the tracker's
# Gitea instance, reachable only over the owner's tailnet (ADR-0029).
#
# The literal below is the guard's own exception to its own rule — the ban must
# name what it bans — so it is exempt here and nowhere else; see ``EXEMPT``.
#
# A hostname is not a path root, so it needs its own matcher. The scheme of a
# ``https://host/…`` URL makes the leading token look like an absolute path, so
# the path rule above deliberately lets it through; the host itself is the
# reference, so matching it alone catches the bare host, a backticked host, and
# every scheme-qualified or port-qualified URL form at once.
PRIVATE_HOSTS = ("gitea.tailnet-00e4.ts.net",)

# Files exempt because they define or explain the boundary: the ignore rule
# itself, the standing instructions, the tracker convention, the ADR that moves
# the tracker (which must name the archive it freezes), and this guard.
#
# This guard's membership is the **owner-accepted exception** to the hostname
# ban, not an oversight: a ban has to name what it bans, so ``PRIVATE_HOSTS``
# contains the one committed literal that may stand, and the docstring/comment
# here is the only committed prose that may discuss it. The owner accepted it
# standing when it was raised, on 2026-09-19 — recorded beside the rule in
# ``docs/agents/issue-tracker.md``; a second file naming the host is a finding.
EXEMPT = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "docs/adr/0029-tracker-moves-to-a-private-gitea-instance.md",
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

# Each host matched as a *host token*: a leading boundary so a different name
# ending in it (``notgitea.tailnet-00e4.ts.net``) is not glued on, and a trailing
# boundary so a longer name in either direction is not a hit — neither a wider
# subdomain (``x.…``) nor a longer domain (``….evil``). A dot that ends the
# token rather than continuing it is allowed, because that is how a sentence
# ends and how an FQDN may be rooted.
#
# ``IGNORECASE`` because a hostname is case-insensitive: ``GITEA.….TS.NET`` is
# the same address, so matching one spelling would be a silent bypass. Path
# roots above are deliberately *not* matched this way — they have to resolve on
# the case-sensitive machine that opens the published link.
_HOST = {
    host: re.compile(
        rf"(?<![A-Za-z0-9.-]){re.escape(host)}(?![A-Za-z0-9-])(?!\.[A-Za-z0-9-])",
        re.IGNORECASE,
    )
    for host in PRIVATE_HOSTS
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


def _references_host(line: str, host: str) -> bool:
    """True when *line* names *host*, bare or inside a URL.

    Unlike a path root this needs no repo-relative test. ``https://host/…`` is
    exactly the qualified form that slips past the path rule — its leading token
    looks absolute — and the host is what a public reader cannot resolve in any
    form. A bare host, a backticked host, and every scheme- or port-qualified
    URL contain the same hostname, so one match covers them all.
    """
    return _HOST[host].search(line) is not None


def _offences(name: str, text: str) -> list[str]:
    """Lines of *name* that reference an ignored path root or a private host."""
    return [
        f"{name}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if any(_references_root(line, root) for root in IGNORED_PATH_ROOTS)
        or any(_references_host(line, host) for host in PRIVATE_HOSTS)
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
    """No committed file outside the exempt set names the tracker's address.

    That is an ignored path root or the private instance's hostname. Durable
    documents cite durable sources: if a tracker entry carries a finding worth
    citing, it graduates into ``docs/`` and *that* is what gets linked.
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
        "committed files must not reference the unpublished tracker "
        f"({', '.join((*IGNORED_PATH_ROOTS, *PRIVATE_HOSTS))}). Name the lane in "
        "prose, cite a durable source, or graduate the finding into docs/. See "
        "docs/agents/issue-tracker.md § Citing the tracker from committed "
        "files.\n" + "\n".join(violations)
    )


def test_the_host_match_is_case_insensitive() -> None:
    """A hostname is case-insensitive, so a shouty spelling is the same address.

    ``GITEA.TAILNET-00E4.TS.NET`` reaches the instance while reading as a
    different string, so matching only the lower-case spelling is a silent
    bypass of the whole rule. A path root is deliberately *not* matched this
    way: it has to resolve on the case-sensitive machine that opens the link.
    """
    for host in PRIVATE_HOSTS:
        assert _references_host(f"see https://{host.upper()}/zhaow/x", host), host
