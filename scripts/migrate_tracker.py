#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Import the gitignored local tracker into Gitea issues + wiki pages.

One-shot and re-runnable: the local markdown tracker (docs/agents/issue-tracker.md)
was retired in favour of Gitea issues, and this is the migration that moves it.

* One issue per ``<lane>/issues/NN-slug.md``. The tracker file is the body of the
  issue, verbatim, behind a provenance marker so a second run creates nothing:
  ``<!-- scratch:<lane>/<relpath> sha=<12 hex> -->`` is the first line, and the
  footer names the origin and the original sha.
* One wiki page per ``<lane>/spec.md`` (title ``<lane>/spec``) and per other doc
  (title ``<lane>/<stem>``). Nested docs keep their stem only — the mapping is
  ``<lane>/<stem>``, so ``console-ia/spec/02-auth.md`` becomes
  ``console-ia/02-auth`` and Gitea treats the slash as a subpage.
* Labels: ``lane/<lane>`` (stable hash-derived colour), ``type/<t>``,
  ``from/scratch``, plus the triage role bare (``ready-for-agent`` …) when the
  ``**Status:**`` value is one of the five roles, else ``status/<value>``. A
  status outside the vocabulary is never promoted to a triage role.
* ``done``, ``wontfix`` and ``resolved`` are created and then closed.

Dry-run is the **default** and makes no network call at all — it only parses the
tracker and writes a manifest. ``--apply`` is the only mode that talks to Gitea;
it needs a token in ``$GITEA_TOKEN`` or the ``tea`` login store. The token is
never printed, and it is never written to the manifest. Every request goes
through an opener built with ``ProxyHandler({})``, so an ``http_proxy`` in the
environment can never intercept the target host.

The tracker is environment-local (ADR-0006): this script reads it, never commits
it, and never invents content — every issue body and wiki page is the source file.
Its directory is deliberately not defaulted to a path, because the convention keeps
that path out of committed files (``docs/agents/issue-tracker.md``): pass
``--tracker DIR``, or set ``CLEAR_RECORD_TRACKER_DIR`` for a scripted run.

Usage (normally via ``just migrate-tracker …``)::

    uv run --no-project scripts/migrate_tracker.py --tracker DIR            # dry run
    uv run --no-project scripts/migrate_tracker.py --tracker DIR --only LANE
    uv run --no-project scripts/migrate_tracker.py --tracker DIR --apply    # import

Exit status is 0 on success and non-zero if any per-item call failed; the rest of
the import still runs, and the summary names every failure.
"""

from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://gitea.tailnet-00e4.ts.net"
DEFAULT_REPO = "zhaow/clear-record"

# The tracker root is named by the caller, never defaulted to a path here: the
# local tracker convention keeps that path out of committed files
# (docs/agents/issue-tracker.md, and packages/clear-record/tests/test_tracker_refs.py
# enforces it). The variable is the scripted-run form of `--tracker DIR`.
TRACKER_ENV = "CLEAR_RECORD_TRACKER_DIR"

TEA_CONFIG = Path.home() / "Library/Application Support/tea/config.yml"
TEA_LOGIN = "zhaow"

# The five canonical triage roles (docs/agents/triage-labels.md). Any other
# status value becomes `status/<value>` instead — never a triage role.
TRIAGE_ROLES = (
    "needs-triage",
    "needs-info",
    "ready-for-agent",
    "ready-for-human",
    "wontfix",
)
TRIAGE_COLOURS = {
    "needs-triage": "#fbca04",
    "needs-info": "#d4c5f9",
    "ready-for-agent": "#0e8a16",
    "ready-for-human": "#1d76db",
    "wontfix": "#cccccc",
}
FROM_LABEL = "from/scratch"
FROM_COLOUR = "#8250df"
STATUS_COLOUR = "#bfd4f2"

# Statuses that mean the work is over: create the issue, then close it.
CLOSE_STATES = frozenset({"done", "wontfix", "resolved"})

REQUEST_TIMEOUT = 30
REQUEST_PAUSE = 0.05  # the instance is small and local: stay gentle, stay serial

MARKER_RE = re.compile(r"<!--\s*scratch:(?P<key>\S+)\s+sha=(?P<sha>[0-9a-f]{12})\s*-->")
COMMENTS_HEADING_RE = re.compile(r"^##[ \t]+Comments[ \t]*$", re.M)
H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)


class GiteaError(Exception):
    """A Gitea call failed; the caller decides whether that aborts the run."""

    def __init__(self, status: int, method: str, url: str, message: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {message}")
        self.status = status


# --------------------------------------------------------------------------- #
# Parsing the tracker
# --------------------------------------------------------------------------- #


@dataclass
class Issue:
    lane: str
    relpath: str  # "issues/01-slug.md", relative to the lane
    path: Path
    title: str
    sha: str
    status: str | None
    type_: str | None
    labels: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # ["02", ...] as written
    content: str = ""  # the file, minus the `## Comments` section
    comments: str | None = None  # everything after `## Comments`, if any

    @property
    def key(self) -> str:
        return f"{self.lane}/{self.relpath}"

    @property
    def closes(self) -> bool:
        return self.status in CLOSE_STATES


@dataclass
class Doc:
    lane: str
    relpath: str
    path: Path
    title: str
    sha: str
    text: str

    @property
    def key(self) -> str:
        return f"{self.lane}/{self.relpath}"

    @property
    def is_spec(self) -> bool:
        return self.relpath == "spec.md"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


FIELD_MARKER_RE = re.compile(r"\*\*[A-Za-z][A-Za-z0-9 -]*:\*\*")
HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]")
BULLET_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]|\d+\.[ \t])")


def metadata_lines(text: str) -> list[str]:
    """The front-matter lines of a ticket: below the H1, above the first heading.

    Tracker front matter is not uniform — the fields sit anywhere in the first
    screenful — so the region is bounded by structure, not by a line count.
    """
    lines = text.split("\n")
    start = 1 if lines and HEADING_LINE_RE.match(lines[0]) else 0
    for offset, line in enumerate(lines[start:], start):
        if HEADING_LINE_RE.match(line):
            return lines[start:offset]
    return lines[start:]


def field_value(text: str, name: str) -> str | None:
    """The value of ``**Name:**`` in the front matter, if the file has one.

    The three fields are often fused onto one line (``**Type:** task. **Status:**
    ready-for-agent. **Blocked by:** None.``), so the value runs to the next bold
    field marker, not to the end of the line. A bullet-led line — a design
    section listing ``- **Type:** system-native stack`` — is not front matter.
    """
    needle = f"**{name}:**"
    for line in metadata_lines(text):
        if BULLET_RE.match(line):
            continue
        start = line.find(needle)
        if start == -1:
            continue
        rest = line[start + len(needle) :]
        following = FIELD_MARKER_RE.search(rest)
        if following:
            rest = rest[: following.start()]
        value = rest.strip()
        if value:
            return value
    return None


def status_token(value: str | None) -> str | None:
    """``done — findings in …`` and ``ready-for-agent.`` both yield one token."""
    if value is None:
        return None
    match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)", value)
    return match.group(1).lower() if match else None


def type_token(value: str | None) -> str | None:
    """``task (refactor)`` is a refactor: the qualifier is the specific type."""
    if value is None:
        return None
    match = re.match(r"([A-Za-z][A-Za-z0-9-]*)[ \t]*(?:\(([a-z][a-z0-9-]*)\))?", value)
    if match is None:
        return None
    return match.group(2) or match.group(1).lower()


def blocked_refs(value: str | None) -> list[str]:
    """Ticket numbers from a ``**Blocked by:**`` value, in the order written.

    Only the leading clause counts: the value is cut at the first sentence end
    (or the next ``**Field:``), so ``**Blocked by:** None. (Consumes results of
    01/02 …)`` stays "no blockers" and ``**Blocked by:** 03 (htmx 4). **Blocks:**
    None.`` yields ``03`` without picking up the ``4``.
    """
    if value is None:
        return []
    head = re.split(r"(?<=\.)\s|\*\*", value, maxsplit=1)[0].strip()
    if not head or head in {"—", "-", "–"} or head.lower().startswith("none"):
        return []
    refs: list[str] = []
    for token in re.split(r",|\band\b", head):
        match = re.match(r"\s*(\d{1,3})\b", token)
        if match:
            refs.append(match.group(1))
    return refs


def labels_for(lane: str, type_: str | None, status: str | None) -> list[str]:
    labels = [f"lane/{lane}", FROM_LABEL]
    if type_:
        labels.append(f"type/{type_}")
    if status in TRIAGE_ROLES:
        labels.append(status)
    elif status:
        labels.append(f"status/{status}")
    return labels


def parse_issue(path: Path, tracker: Path, lane: str) -> Issue:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    relpath = path.relative_to(tracker / lane).as_posix()

    heading = H1_RE.search(text)
    title = heading.group(1).strip() if heading else path.stem.replace("-", " ")

    content, comments = text, None
    heading_match = COMMENTS_HEADING_RE.search(text)
    if heading_match:
        content = text[: heading_match.start()].rstrip("\n")
        tail = text[heading_match.end() :].strip("\n")
        comments = tail or None

    status = status_token(field_value(text, "Status"))
    type_ = type_token(field_value(text, "Type"))
    return Issue(
        lane=lane,
        relpath=relpath,
        path=path,
        title=title,
        sha=sha12(raw),
        status=status,
        type_=type_,
        labels=labels_for(lane, type_, status),
        blocked=blocked_refs(field_value(text, "Blocked by")),
        content=content,
        comments=comments,
    )


def parse_doc(path: Path, tracker: Path, lane: str) -> Doc:
    relpath = path.relative_to(tracker / lane).as_posix()
    text = path.read_text(encoding="utf-8")
    title = f"{lane}/spec" if relpath == "spec.md" else f"{lane}/{path.stem}"
    return Doc(
        lane=lane,
        relpath=relpath,
        path=path,
        title=title,
        sha=sha12(path.read_bytes()),
        text=text,
    )


def parse_tracker(
    tracker: Path, only: str | None
) -> tuple[list[str], list[Issue], list[Doc]]:
    if not tracker.is_dir():
        raise SystemExit(
            f"migrate-tracker: no tracker at {tracker}\n"
            "  Pass --tracker DIR (the local tracker is gitignored, so a fresh "
            "worktree does not have one)."
        )

    lanes = sorted(
        entry.name
        for entry in tracker.iterdir()
        if entry.is_dir() and any(entry.rglob("*.md"))
    )
    if only is not None:
        if only not in lanes:
            raise SystemExit(
                f"migrate-tracker: --only {only}: no such lane\n"
                f"  lanes: {', '.join(lanes)}"
            )
        lanes = [only]

    issues: list[Issue] = []
    docs: list[Doc] = []
    for lane in lanes:
        lane_dir = tracker / lane
        issues += [
            parse_issue(p, tracker, lane)
            for p in sorted((lane_dir / "issues").glob("*.md"))
        ]
        others = [
            path
            for path in sorted(lane_dir.rglob("*.md"))
            if path.relative_to(lane_dir).parts[0] != "issues"
        ]
        docs += [parse_doc(path, tracker, lane) for path in others]

    titles = collections.Counter(doc.title for doc in docs)
    collisions = sorted(title for title, count in titles.items() if count > 1)
    if collisions:
        raise SystemExit(
            "migrate-tracker: wiki titles collide, so the mapping is ambiguous: "
            + ", ".join(collisions)
        )
    return lanes, issues, docs


# --------------------------------------------------------------------------- #
# Manifest and reporting
# --------------------------------------------------------------------------- #


def summarize(lanes: list[str], issues: list[Issue], docs: list[Doc]) -> dict[str, Any]:
    statuses = collections.Counter(issue.status or "(none)" for issue in issues)
    specs = [doc for doc in docs if doc.is_spec]
    return {
        "lanes": len(lanes),
        "markdown_files": len(issues) + len(docs),
        "issues": len(issues),
        "issue_status": dict(sorted(statuses.items(), key=lambda kv: (-kv[1], kv[0]))),
        "issues_with_comments": sum(1 for issue in issues if issue.comments),
        "blocked_by_refs": sum(len(issue.blocked) for issue in issues),
        "closes": sum(1 for issue in issues if issue.closes),
        "wiki_pages": len(docs),
        "wiki_spec_pages": len(specs),
        "wiki_other_pages": len(docs) - len(specs),
    }


def label_colour(name: str) -> str:
    if name.startswith("lane/"):
        # Stable, hash-derived: the same lane always gets the same colour.
        return "#" + hashlib.sha256(name[5:].encode()).hexdigest()[:6]
    if name == FROM_LABEL:
        return FROM_COLOUR
    if name.startswith("status/"):
        return STATUS_COLOUR
    return TRIAGE_COLOURS.get(name, "#ededed")


def labels_needed(issues: list[Issue]) -> dict[str, str]:
    names: set[str] = set()
    for issue in issues:
        names.update(issue.labels)
    return {name: label_colour(name) for name in sorted(names)}


def manifest_payload(
    args: argparse.Namespace,
    tracker: Path,
    lanes: list[str],
    issues: list[Issue],
    docs: list[Doc],
    numbers: dict[str, int],
    outcome: dict[str, Any] | None,
) -> dict[str, Any]:
    lookup = ref_lookup(issues)
    return {
        "generated": date.today().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "tracker": str(tracker),
        "repo": args.repo,
        "url": args.url,
        "only": args.only,
        "counts": summarize(lanes, issues, docs),
        "labels": labels_needed(issues),
        "issues": [
            {
                "key": issue.key,
                "title": issue.title,
                "status": issue.status,
                "type": issue.type_,
                "labels": issue.labels,
                "blocked_by": issue.blocked,
                "blocked_by_issues": [
                    numbers[sibling.key]
                    for ref in issue.blocked
                    if (sibling := lookup.get(sibling_key(issue, ref))) is not None
                    and sibling.key in numbers
                ],
                "comments": bool(issue.comments),
                "closes": issue.closes,
                "sha": issue.sha,
                "number": numbers.get(issue.key),
            }
            for issue in issues
        ],
        "wiki": [
            {
                "key": doc.key,
                "title": doc.title,
                "spec": doc.is_spec,
                "sha": doc.sha,
            }
            for doc in docs
        ],
        "outcome": outcome,
    }


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def print_report(
    tracker: Path,
    args: argparse.Namespace,
    counts: dict[str, Any],
    labels: dict[str, str],
    manifest_path: Path,
) -> None:
    mode = "apply" if args.apply else "dry run (no network)"
    scope = f", --only {args.only}" if args.only else ""
    print(f"migrate-tracker: {mode} — {tracker} -> {args.repo} at {args.url}{scope}")
    print(f"  lanes                  {counts['lanes']}")
    print(
        f"  markdown files         {counts['markdown_files']}"
        f"  ({counts['issues']} issue files"
        f" + {counts['wiki_spec_pages']} spec.md"
        f" + {counts['wiki_other_pages']} other docs)"
    )
    statuses = ", ".join(f"{name} {n}" for name, n in counts["issue_status"].items())
    print(f"  issue statuses         {statuses}")
    print(
        f"  issues to close        {counts['closes']}"
        f"   comments to post {counts['issues_with_comments']}"
        f"   blocked-by refs {counts['blocked_by_refs']}"
    )
    print(f"  wiki pages             {counts['wiki_pages']}")
    print(f"  labels                 {len(labels)}: {', '.join(labels)}")
    print(f"  manifest               {manifest_path}")


# --------------------------------------------------------------------------- #
# Gitea
# --------------------------------------------------------------------------- #


def read_tea_token(path: Path, login: str) -> str | None:
    """Pull one login's token out of tea's YAML config, without a YAML parser.

    The file is a flat ``logins:`` list of ``- name:`` entries; a handful of
    regexes is enough for it and keeps this script stdlib-only. The token is
    returned, never printed.
    """
    if not path.is_file():
        return None
    name: str | None = None
    token: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = re.match(r"^\s*-\s*name:\s*(\S+)\s*$", line)
        if entry:
            if name == login and token:
                return token
            name, token = entry.group(1), None
            continue
        if name is None:
            continue
        value = re.match(r"^\s*token:\s*(.+?)\s*$", line)
        if value:
            token = value.group(1).strip().strip("'\"")
    return token if name == login else None


def resolve_token() -> tuple[str, str]:
    """The token and a printable description of where it came from."""
    env = os.environ.get("GITEA_TOKEN", "").strip()
    if env:
        return env, "$GITEA_TOKEN"
    tea = read_tea_token(TEA_CONFIG, TEA_LOGIN)
    if tea:
        return tea, f"tea login '{TEA_LOGIN}' ({TEA_CONFIG})"
    raise SystemExit(
        "migrate-tracker: no Gitea token.\n"
        f"  Set GITEA_TOKEN, or run tea login add for a login named '{TEA_LOGIN}' "
        f"({TEA_CONFIG})."
    )


class Gitea:
    def __init__(self, url: str, repo: str, token: str) -> None:
        self.api = f"{url.rstrip('/')}/api/v1/repos/{repo}"
        self.token = token
        # Never let a configured proxy intercept the tailnet host.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        url: str,
        payload: Any = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"token {self.token}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "clear-record-migrate-tracker")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
            display = urllib.parse.urlsplit(url)
            raise GiteaError(
                exc.code,
                method,
                f"{display.path}?{display.query}" if display.query else display.path,
                detail,
            ) from None
        except urllib.error.URLError as exc:
            raise GiteaError(0, method, url, f"{exc.reason}") from None
        if status not in ok:
            raise GiteaError(status, method, url, "unexpected status")
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    # -- labels ------------------------------------------------------------ #

    def labels(self) -> dict[str, int]:
        found: dict[str, int] = {}
        page = 1
        while True:
            batch = self.request("GET", f"{self.api}/labels?limit=50&page={page}")
            for label in batch:
                found[label["name"]] = label["id"]
            if len(batch) < 50:
                return found
            page += 1

    def ensure_label(
        self, name: str, colour: str, known: dict[str, int]
    ) -> tuple[int, bool]:
        """Create the label if absent (tolerating a race), return (id, created)."""
        if name in known:
            return known[name], False
        try:
            label = self.request(
                "POST", f"{self.api}/labels", {"name": name, "color": colour}
            )
            known[name] = label["id"]
            return label["id"], True
        except GiteaError as exc:
            if exc.status not in (409, 422):  # already exists on an older instance
                raise
            known.update({k: v for k, v in self.labels().items() if k not in known})
            if name not in known:
                raise
            return known[name], False

    # -- issues ------------------------------------------------------------ #

    def existing_issues(self) -> dict[str, dict[str, Any]]:
        """key -> the issue already imported for that ticket, in any state."""
        found: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            batch = self.request(
                "GET",
                f"{self.api}/issues?state=all&type=issues&limit=50&page={page}",
            )
            for issue in batch:
                # Gitea carries the key with a null value on real issues, so
                # presence is not the test — a pull request has an object here.
                if issue.get("pull_request"):
                    continue
                match = MARKER_RE.search(issue.get("body") or "")
                if match:
                    found.setdefault(match.group("key"), issue)
            if len(batch) < 50:
                return found
            page += 1

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request(
                "GET", f"{self.api}/issues/{number}/comments?limit=50&page={page}"
            )
            comments += batch
            if len(batch) < 50:
                return comments
            page += 1

    def create_issue(self, title: str, body: str, label_ids: list[int]) -> int:
        issue = self.request(
            "POST",
            f"{self.api}/issues",
            {"title": title, "body": body, "labels": label_ids},
        )
        return issue["number"]

    def patch_issue(self, number: int, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"{self.api}/issues/{number}", payload)

    def comment(self, number: int, body: str) -> None:
        self.request("POST", f"{self.api}/issues/{number}/comments", {"body": body})

    # -- wiki -------------------------------------------------------------- #

    def wiki_index(self) -> dict[str, str]:
        """title -> ``sub_url`` for every wiki page.

        Landed titles are not always addressable verbatim: Gitea escapes the
        slash of a subpage and appends ``.-`` to a name it reads as a date
        (``console-ia/02-auth`` becomes ``console-ia%2F02-auth.-``). So pages are
        looked up through this index and then addressed by ``sub_url``.
        """
        index: dict[str, str] = {}
        page = 1
        while True:
            try:
                batch = self.request(
                    "GET", f"{self.api}/wiki/pages?limit=50&page={page}"
                )
            except GiteaError as exc:
                if exc.status == 404 and page == 1:  # no wiki yet
                    return index
                raise
            for entry in batch:
                index[entry["title"]] = entry["sub_url"]
            if len(batch) < 50:
                return index
            page += 1

    def wiki_page(self, sub_url: str) -> str:
        page = self.request("GET", f"{self.api}/wiki/page/{sub_url}")
        encoded = page.get("content_base64")
        return base64.b64decode(encoded).decode("utf-8") if encoded else ""

    def create_wiki(self, title: str, content: str) -> None:
        self.request(
            "POST",
            f"{self.api}/wiki/new",
            {
                "title": title,
                "content_base64": base64.b64encode(content.encode("utf-8")).decode(
                    "ascii"
                ),
                "message": f"Import {title} from the retired local tracker",
            },
        )

    def edit_wiki(self, sub_url: str, title: str, content: str) -> None:
        self.request(
            "PATCH",
            f"{self.api}/wiki/page/{sub_url}",
            {
                "title": title,
                "content_base64": base64.b64encode(content.encode("utf-8")).decode(
                    "ascii"
                ),
                "message": f"Re-import {title} from the retired local tracker",
            },
        )


# --------------------------------------------------------------------------- #
# Body construction
# --------------------------------------------------------------------------- #


def marker(issue: Issue) -> str:
    return f"<!-- scratch:{issue.key} sha={issue.sha} -->"


def footer(issue: Issue) -> str:
    line = (
        f"Imported from the retired local tracker ({issue.key}) on "
        f"{date.today().isoformat()}; original sha {issue.sha}."
    )
    if issue.comments:
        line += (
            " The tracker's `## Comments` section was not copied into this body; "
            "it is posted as this issue's first comment."
        )
    return line


def blocker_line(
    issue: Issue, numbers: dict[str, int], lookup: dict[str, Issue]
) -> str | None:
    resolved: list[int] = []
    for ref in issue.blocked:
        sibling = lookup.get(sibling_key(issue, ref))
        if sibling is not None and sibling.key in numbers:
            resolved.append(numbers[sibling.key])
    if not resolved:
        return None
    return "Blocked by: " + ", ".join(f"#{number}" for number in resolved)


def build_body(issue: Issue, numbers: dict[str, int], lookup: dict[str, Issue]) -> str:
    parts = [marker(issue), "", issue.content]
    blocked = blocker_line(issue, numbers, lookup)
    if blocked:
        parts += ["", blocked]
    parts += ["", footer(issue)]
    return "\n".join(parts) + "\n"


def sibling_key(issue: Issue, ref: str) -> str:
    """The plan key of the ticket a ``Blocked by: NN`` line points at."""
    return f"{issue.lane}/issues/{ref}"


def ref_lookup(issues: list[Issue]) -> dict[str, Issue]:
    """``<lane>/issues/<relpath>`` and ``<lane>/issues/NN`` -> the sibling issue."""
    lookup: dict[str, Issue] = {}
    for issue in issues:
        lookup[issue.key] = issue
        match = re.match(r"issues/(\d+)", issue.relpath)
        if match:
            lookup[f"{issue.lane}/issues/{match.group(1)}"] = issue
    return lookup


def reconcile_issue(
    client: Gitea,
    issue: Issue,
    remote: dict[str, Any],
    numbers: dict[str, int],
    lookup: dict[str, Issue],
) -> list[str]:
    """Bring an issue a previous run created back in line with the tracker.

    The marker is written with the body, so finding one proves only that the
    *issue* was created — not that the side effects that follow it landed. A
    transient error between those two points used to strand the ticket: the
    run after it saw the marker and skipped, so the tracker's comments, the
    closed state, or a ``Blocked by:`` line was lost for good. Each side effect
    is therefore checked here and repaired, and the repairs are reported.
    """
    number = remote["number"]
    repairs: list[str] = []
    if issue.comments:
        wanted = issue.comments.strip("\n")
        posted = any(
            (comment.get("body") or "").strip("\n") == wanted
            for comment in client.issue_comments(number)
        )
        if not posted:
            client.comment(number, issue.comments)
            repairs.append("comment")
        time.sleep(REQUEST_PAUSE)
    if (remote.get("state") == "closed") != issue.closes:
        client.patch_issue(number, {"state": "closed" if issue.closes else "open"})
        repairs.append("closed" if issue.closes else "reopened")
        time.sleep(REQUEST_PAUSE)
    blocked = blocker_line(issue, numbers, lookup)
    if blocked is not None and blocked not in (remote.get("body") or ""):
        client.patch_issue(number, {"body": build_body(issue, numbers, lookup)})
        repairs.append("blocked-by")
        time.sleep(REQUEST_PAUSE)
    return repairs


# --------------------------------------------------------------------------- #
# The two modes
# --------------------------------------------------------------------------- #


def run_dry(
    tracker: Path,
    args: argparse.Namespace,
    lanes: list[str],
    issues: list[Issue],
    docs: list[Doc],
    manifest_path: Path,
) -> int:
    counts = summarize(lanes, issues, docs)
    labels = labels_needed(issues)
    print_report(tracker, args, counts, labels, manifest_path)
    payload = manifest_payload(args, tracker, lanes, issues, docs, {}, None)
    write_manifest(manifest_path, payload)
    return 0


def run_apply(
    tracker: Path,
    args: argparse.Namespace,
    lanes: list[str],
    issues: list[Issue],
    docs: list[Doc],
    manifest_path: Path,
) -> int:
    token, source = resolve_token()
    print(f"migrate-tracker: token from {source} (not printed)")
    client = Gitea(args.url, args.repo, token)

    failures: list[str] = []
    try:
        existing = client.existing_issues()
        known_labels = client.labels()
        wiki_index = client.wiki_index()
    except GiteaError as exc:
        raise SystemExit(
            f"migrate-tracker: cannot read {args.repo} at {args.url} — {exc}\n"
            "  --apply assumes the target repository exists and the token may read it."
        ) from None
    numbers: dict[str, int] = {key: issue["number"] for key, issue in existing.items()}
    wanted = labels_needed(issues)

    created_labels = 0
    for name, colour in wanted.items():
        try:
            _, created = client.ensure_label(name, colour, known_labels)
            created_labels += int(created)
        except GiteaError as exc:
            failures.append(f"label {name}: {exc}")

    lookup = ref_lookup(issues)
    created = present = reconciled = 0
    deferred: list[Issue] = []
    for issue in issues:
        remote = existing.get(issue.key)
        if remote is not None:
            present += 1
            try:
                repairs = reconcile_issue(client, issue, remote, numbers, lookup)
            except GiteaError as exc:
                failures.append(f"issue {issue.key} (reconcile): {exc}")
                continue
            if repairs:
                reconciled += 1
                print(
                    f"  issue #{remote['number']:<4} {issue.key} "
                    f"reconciled: {', '.join(repairs)}"
                )
            continue
        pending_refs = [
            ref
            for ref in issue.blocked
            if (sibling := lookup.get(sibling_key(issue, ref))) is not None
            and sibling.key not in numbers
        ]
        try:
            number = client.create_issue(
                issue.title,
                build_body(issue, numbers, lookup),
                [known_labels[name] for name in issue.labels if name in known_labels],
            )
            numbers[issue.key] = number
            created += 1
            print(f"  issue #{number:<4} {issue.key} [{', '.join(issue.labels)}]")
            if issue.comments:
                client.comment(number, issue.comments)
            if issue.closes:
                client.patch_issue(number, {"state": "closed"})
            if pending_refs:
                deferred.append(issue)
        except GiteaError as exc:
            failures.append(f"issue {issue.key}: {exc}")
        time.sleep(REQUEST_PAUSE)

    for issue in deferred:
        number = numbers[issue.key]
        if blocker_line(issue, numbers, lookup) is None:
            print(f"  issue #{number:<4} {issue.key} blocked-by left unresolved")
            continue
        try:
            client.patch_issue(number, {"body": build_body(issue, numbers, lookup)})
        except GiteaError as exc:
            failures.append(f"issue {issue.key} (blocked-by): {exc}")
            continue
        print(f"  issue #{number:<4} {issue.key} blocked-by resolved later")
        time.sleep(REQUEST_PAUSE)

    wiki_created = wiki_updated = wiki_unchanged = 0
    for doc in docs:
        try:
            sub_url = wiki_index.get(doc.title)
            if sub_url is None:
                client.create_wiki(doc.title, doc.text)
                wiki_created += 1
                print(f"  wiki  new    {doc.title} ({doc.key})")
            elif client.wiki_page(sub_url).rstrip("\n") != doc.text.rstrip("\n"):
                client.edit_wiki(sub_url, doc.title, doc.text)
                wiki_updated += 1
                print(f"  wiki  update {doc.title} ({doc.key})")
            else:
                wiki_unchanged += 1
        except GiteaError as exc:
            failures.append(f"wiki {doc.key}: {exc}")
        time.sleep(REQUEST_PAUSE)

    unresolved = collections.Counter(
        ref
        for issue in issues
        for ref in issue.blocked
        if f"{issue.lane}/issues/{ref}" not in lookup
    )

    outcome = {
        "labels_created": created_labels,
        "issues_created": created,
        "issues_already_present": present,
        "issues_reconciled": reconciled,
        "issues_deferred_body_patch": len(deferred),
        "wiki_created": wiki_created,
        "wiki_updated": wiki_updated,
        "wiki_unchanged": wiki_unchanged,
        "failures": failures,
    }
    print()
    print(
        f"migrate-tracker: apply — labels +{created_labels}, "
        f"issues created {created} / already present {present} "
        f"({reconciled} reconciled), "
        f"wiki created {wiki_created} / updated {wiki_updated} / unchanged {wiki_unchanged}"
    )
    if unresolved:
        print(f"  unresolved blocked-by refs: {dict(unresolved)}")
    if failures:
        print(f"  failures ({len(failures)}):")
        for failure in failures:
            print(f"    - {failure}")

    payload = manifest_payload(args, tracker, lanes, issues, docs, numbers, outcome)
    write_manifest(manifest_path, payload)
    print(f"  manifest {manifest_path}")

    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_tracker.py",
        description=(
            "Import the gitignored local markdown tracker into Gitea issues + "
            "wiki pages. Dry-run unless --apply is passed."
        ),
    )
    parser.add_argument(
        "--tracker",
        metavar="DIR",
        help=f"tracker root; required unless ${TRACKER_ENV} names it",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        metavar="OWNER/NAME",
        help=f"target repository (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        metavar="URL",
        help=f"Gitea base URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the import (writes to Gitea); default is a dry run with no network",
    )
    parser.add_argument("--only", metavar="LANE", help="limit the run to one lane")
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        help="manifest to write (default: <repo>/.local/migrate-tracker-manifest.json)",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    # `just migrate-tracker -- --apply` is the usual way to hand flags through a
    # recipe; argparse would read everything after `--` as positional, and this
    # script has no positionals, so drop the separator.
    return parser.parse_args([arg for arg in raw if arg != "--"])


def resolve_tracker(args: argparse.Namespace) -> Path:
    """The tracker root: ``--tracker DIR``, else ``$CLEAR_RECORD_TRACKER_DIR``.

    Failing loudly beats guessing: the tracker holds environment-local data
    (ADR-0006), and the tool that reads it should be pointed at it by the human
    who owns it rather than scanning the working tree for something plausible.
    """
    if args.tracker:
        return Path(args.tracker).expanduser().resolve()
    from_env = os.environ.get(TRACKER_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser().resolve()
    raise SystemExit(
        "migrate-tracker: no tracker directory.\n"
        f"  Pass --tracker DIR, or set {TRACKER_ENV}. The tracker is gitignored\n"
        "  and environment-local, so nothing here knows where it is; see\n"
        "  docs/agents/issue-tracker.md for the convention."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tracker = resolve_tracker(args)
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else repo_root() / ".local/migrate-tracker-manifest.json"
    )

    lanes, issues, docs = parse_tracker(tracker, args.only)
    if args.apply:
        return run_apply(tracker, args, lanes, issues, docs, manifest_path)
    return run_dry(tracker, args, lanes, issues, docs, manifest_path)


if __name__ == "__main__":
    sys.exit(main())
