#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Check that a tracker dump holds the tracker, and say what it holds.

The tracker lives on a private Gitea instance, and the one record of it is that
instance's database (ADR-0029). ``gitea dump`` inside the container is the lever
that puts a copy of it somewhere else; this script is the check that the copy is
really the tracker, rather than a file that merely looks like one.

It reads the dump **offline** — the dump is a zip, the database inside it is an
xorm-migrated SQLite file, and stdlib ``sqlite3`` opens it — and counts what the
dump contains: tickets (issues, pull requests excluded), comments, wiki pages,
user accounts, labels and attachments. The wiki is not in the database: Gitea
keeps it as a git repository under ``repos/<owner>/<repo>.wiki.git``, so its page
list comes from the tree at that repository's HEAD.

When a source instance is named (``--url URL`` or ``$CLEAR_RECORD_GITEA_URL``) the
same counts are fetched from the instance's API and compared:

* **counts**, entity by entity — tickets, comments, wiki pages, users, labels,
  attachments — and the *names* the tracker is made of: which issue numbers, wiki
  page titles and label names the dump has, which only the source has, which only
  the dump has. Labels are compared because nothing else carries them: a restore
  can drop every label and still return every ticket and comment;
* **a sample of issues field by field** — title, state, body digest, created and
  updated timestamps, comment count, attachment count. The sample is
  deterministic: the lowest N and the highest N issue numbers, so two runs over
  the same dump sample the same tickets and both ends of the history are covered.

The verdict separates the two questions a comparison can answer:

* ``FAIL`` — the dump holds something the source does not. History the dump
  carries is **missing** from the instance it names: a restore that dropped
  tickets, comments or pages, or a dump that is not from the instance it is
  compared against. Exit status 1.
* ``DRIFT`` — the dump holds everything it should, and the source has **moved on
  since it was taken**: tickets created, comments added, fields edited afterwards.
  Expected when the source is the live instance, and the whole point when the
  source is a fresh restore: a restored copy of a dump has had no chance to move
  on, so a drift line in a drill run is a failed restore. Exit status 0.
* ``MATCH`` — the two agree, count for count and field for field.

Every run writes a JSON record (``--record``, by default
``<repo>/.local/tracker-backup-audit.json``) naming the dump by sha256, its size,
the counts, the sampled issues and the verdict. That file is the evidence that a
run happened and what it found; it lives in ``.local/`` with the environment,
because it names the instance it compared against. It is never committed.

The dump path is required and never defaulted: dumps are environment-local, kept
outside the repository's history. The instance URL is not defaulted either, and
for the same reason ``scripts/migrate_tracker.py`` does not default it — the
committed-file guard (``packages/clear-record/tests/test_tracker_refs.py``)
rejects the hostname in any committed file, an error string included. The token
comes from ``$GITEA_TOKEN`` or the ``tea`` login store, is never printed, and is
never written to the record. Reads only: this script never writes to the
instance, and never writes to the dump.

Usage (normally via ``just tracker-backup …``)::

    uv run --no-project scripts/audit_tracker_backup.py --dump FILE
    uv run --no-project scripts/audit_tracker_backup.py --dump FILE --url URL
    uv run --no-project scripts/audit_tracker_backup.py --dump FILE --url URL \\
        --sample 10 --record .local/tracker-backup-audit.json

The round trip this belongs to — take a dump, restore it into a scratch instance,
compare there — is ``docs/tracker-backup.md``. Exit status is 0 for MATCH and
DRIFT, 1 for FAIL, 2 for a missing or unreadable input.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_REPO = "zhaow/clear-record"

# The Gitea base URL is named by the caller, never defaulted to a host here: the
# instance is private, and the committed-file guard
# (packages/clear-record/tests/test_tracker_refs.py) rejects its hostname in any
# committed file — an error string included. Same convention as
# scripts/migrate_tracker.py; the variable is the scripted form of `--url`.
URL_ENV = "CLEAR_RECORD_GITEA_URL"

# Token resolution follows scripts/migrate_tracker.py, including the login name:
# `$GITEA_TOKEN` first, then the tea store. The token is never printed.
TEA_CONFIG = Path.home() / "Library/Application Support/tea/config.yml"
TEA_LOGIN = "zhaow"

# What a dump holds. The database is the whole record *except* the wiki: Gitea
# keeps a wiki as a git repository, not as a table.
DB_MEMBER = "data/gitea.db"
SQLITE_MAGIC = b"SQLite format 3\x00"
WIKI_DIR_SUFFIX = ".wiki.git"

# Gitea escapes a page name into a file name, and appends this to a name it may
# otherwise read as a date (`console-ia/02-auth` -> `console-ia%2F02-auth.-`).
PAGE_NAME_SUFFIX = ".-"

# Issues, pull requests and labels all hang off a repository row, so one scope
# says the same thing to every query: the repository `--repo` named. It is what
# makes the dump's side and the API's side talk about the same tickets — an
# instance holds more than one repository in principle, and the dump's tables do
# not otherwise say which. ``?`` twice: the owner's name, then the repository's.
REPO_SCOPE = (
    "repo_id in (select r.id from repository r"
    " join user u on r.owner_id = u.id"
    " where lower(u.lower_name) = ? and lower(r.lower_name) = ?)"
)

REQUEST_TIMEOUT = 30
PAGE_SIZE = 50

# Rows in Gitea's `comment` table are not all prose: type 0 is a comment a person
# wrote, the rest are the timeline's system events (close, reopen, reference,
# assignee…). `num_comments` on the issue, and the API's comment count, both mean
# type 0 — so that is what the comparison counts.
COMMENT_TYPE_COMMENT = 0

SAMPLE_FIELDS = (
    "title",
    "state",
    "body",
    "created",
    "updated",
    "comments",
    "attachments",
)

TITLE_WIDTH = 58


def die(message: str) -> NoReturn:
    """Report a bad input or a missing prerequisite: stderr, exit status 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------- #
# The instance's API
# --------------------------------------------------------------------------- #


class GiteaError(Exception):
    """A Gitea call failed; the caller decides whether that aborts the run."""

    def __init__(self, status: int, method: str, url: str, message: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {message}")
        self.status = status


def read_tea_token(path: Path, login: str) -> str | None:
    """Pull one login's token out of tea's YAML config, without a YAML parser.

    The file is a flat ``logins:`` list of ``- name:`` entries; a handful of
    regexes is enough for it and keeps this script stdlib-only. The token is
    returned, never printed. The same reader as ``scripts/migrate_tracker.py``.
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
        return tea, f"tea login '{TEA_LOGIN}'"
    die(
        "tracker-backup: no Gitea token.\n"
        f"  Set GITEA_TOKEN, or run tea login add for a login named '{TEA_LOGIN}'\n"
        f"  ({TEA_CONFIG}). The token is needed only to compare against an\n"
        "  instance: a dump-only audit reads no network at all."
    )


class Gitea:
    """The reads a comparison needs, and nothing else.

    Every error carries the request's *path* only: the host is environment-local
    and never printed, so no failure this script reports can spell it out.
    """

    def __init__(self, url: str, repo: str, token: str) -> None:
        self.base = f"{url.rstrip('/')}/api/v1"
        self.repo = repo
        self.token = token
        # Never let a configured proxy intercept the tailnet host.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"token {self.token}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "clear-record-tracker-backup")
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:300]
            raise GiteaError(
                exc.code, "GET", urllib.parse.urlsplit(url).path, detail
            ) from None
        except urllib.error.URLError as exc:
            raise GiteaError(
                0, "GET", urllib.parse.urlsplit(url).path, str(exc.reason)
            ) from None
        return json.loads(body.decode("utf-8")) if body else None

    def get_list(
        self, path: str, params: str = "", key: str | None = None
    ) -> list[Any]:
        """Every entry of a paginated list endpoint."""
        found: list[Any] = []
        page = 1
        while True:
            query = f"{params}&" if params else ""
            payload = self.request(
                f"{self.base}{path}?{query}limit={PAGE_SIZE}&page={page}"
            )
            batch = payload[key] if key else payload
            found += batch
            if len(batch) < PAGE_SIZE:
                return found
            page += 1

    def issues(self) -> dict[int, dict[str, Any]]:
        """number -> the issue, pull requests excluded.

        A pull request is an issue in Gitea's schema and a branch in this
        repository's history, not part of the tracker, so neither side counts
        one.
        """
        found: dict[int, dict[str, Any]] = {}
        for issue in self.get_list(
            f"/repos/{self.repo}/issues", "state=all&type=issues"
        ):
            if issue.get("pull_request"):
                continue
            found[int(issue["number"])] = issue
        return found

    def wiki_titles(self) -> list[str]:
        try:
            pages = self.get_list(f"/repos/{self.repo}/wiki/pages")
        except GiteaError as exc:
            if exc.status == 404:  # no wiki at all
                return []
            raise
        return [str(entry["title"]) for entry in pages]

    def label_names(self) -> list[str]:
        """The repository's labels, as the labeller's side of the tracker."""
        return [
            str(label["name"]) for label in self.get_list(f"/repos/{self.repo}/labels")
        ]

    def user_count(self) -> int:
        """Every account on the instance.

        Site-wide rather than this repository's, because that is the grain a
        database row count can be compared against: a restore that loses accounts
        lost part of the instance.
        """
        return len(self.get_list("/users/search", key="data"))

    def issue_attachments(self, number: int) -> int:
        return len(self.get_list(f"/repos/{self.repo}/issues/{number}/assets"))


# --------------------------------------------------------------------------- #
# Reading the dump
# --------------------------------------------------------------------------- #


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_bytes(size: int) -> str:
    return f"{size / (1 << 20):.1f} MiB" if size >= (1 << 20) else f"{size} bytes"


@dataclass
class DumpIssue:
    db_id: int
    number: int
    title: str
    content: str
    closed: bool
    created_unix: int
    updated_unix: int
    comments: int = 0
    attachments: int = 0

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class DumpWiki:
    directory: str | None
    ref: str | None
    revision: str | None
    pages: dict[str, str] = field(default_factory=dict)  # title -> file name


@dataclass
class DumpFacts:
    path: Path
    sha256: str
    size: int
    members: int
    db_member: str
    db_format: str
    sql_member: str | None
    sql_header: str | None
    issues: dict[int, DumpIssue]
    pull_requests: int
    comment_rows: int
    users: int
    label_names: list[str]
    wiki: DumpWiki

    @property
    def comments(self) -> int:
        return sum(issue.comments for issue in self.issues.values())

    @property
    def attachments(self) -> int:
        return sum(issue.attachments for issue in self.issues.values())


def find_db_member(members: list[str]) -> str | None:
    """The SQLite database inside the dump, by the name ``gitea dump`` gives it."""
    if DB_MEMBER in members:
        return DB_MEMBER
    under_data = sorted(
        name
        for name in members
        if name.startswith("data/") and name.endswith(".db") and "/" not in name[5:]
    )
    return under_data[0] if under_data else None


def find_sql_member(members: list[str]) -> str | None:
    """The dump's SQL-text copy of the database, if it wrote one.

    Top-level only: a ``.sql`` file anywhere else in the zip belongs to one of the
    repositories the dump carries, not to the database.
    """
    for name in members:
        if "/" not in name and name.endswith((".sql", ".sql.gz")):
            return name
    return None


def read_sql_header(archive: zipfile.ZipFile, member: str) -> str | None:
    """The first line of the SQL dump — xorm writes what it is and when."""
    with archive.open(member) as raw:
        stream = gzip.GzipFile(fileobj=raw) if member.endswith(".gz") else raw
        line = stream.readline(200)
    return line.decode("utf-8", "replace").strip() or None


def page_title(filename: str) -> str:
    """A wiki page's title, from the file name Gitea stored it under."""
    stem = filename[: -len(".md")] if filename.endswith(".md") else filename
    if stem.endswith(PAGE_NAME_SUFFIX):
        stem = stem[: -len(PAGE_NAME_SUFFIX)]
    return urllib.parse.unquote(stem)


def read_wiki(
    archive: zipfile.ZipFile, members: list[str], root: Path, repo: str
) -> DumpWiki:
    """The pages in the dump's wiki repository, from the tree at HEAD.

    A wiki is a git repository inside the dump, so the page list is neither a
    table nor a directory listing: it is the tree of the wiki's HEAD commit.
    Reading it that way is what makes the count the *current* pages — a page
    deleted after the dump's moment is still in the repository's history, and
    must not be counted.
    """
    candidates = sorted(
        {
            name.rsplit("/", 1)[0]
            for name in members
            if name.endswith("/HEAD")
            and name.rsplit("/", 1)[0].endswith(WIKI_DIR_SUFFIX)
        }
    )
    wanted = f"repos/{repo}{WIKI_DIR_SUFFIX}"
    directory = wanted if wanted in candidates else next(iter(candidates), None)
    if directory is None:
        return DumpWiki(None, None, None)

    archive.extractall(
        root, members=[name for name in members if name.startswith(f"{directory}/")]
    )
    git_dir = root / directory
    if shutil.which("git") is None:
        die(
            "tracker-backup: `git` is not on PATH.\n"
            f"  The dump's wiki ({directory}) is a git repository, and its page\n"
            "  list is the tree at HEAD; counting pages needs git."
        )

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", "-c", "core.quotepath=false", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    revision = git("rev-parse", "--short", "HEAD")
    try:
        ref = git("symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        ref = None
    listed = git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
    pages: dict[str, str] = {}
    for name in listed:
        if name:
            pages.setdefault(page_title(name), name)
    return DumpWiki(directory, ref, revision, pages)


def split_repo(repo: str) -> tuple[str, str]:
    """``OWNER/NAME`` -> the two lowercase names the dump's own tables carry."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        die(
            f"tracker-backup: --repo must be OWNER/NAME, not {repo!r}.\n"
            "  The same name is looked up in the dump's tables and asked of the\n"
            "  instance's API, so a bare repository name has nothing to match."
        )
    return owner.lower(), name.lower()


def read_issues(conn: sqlite3.Connection, owner: str, name: str) -> list[DumpIssue]:
    rows = conn.execute(
        "select id, `index`, name, content, is_closed, created_unix, updated_unix"
        f" from issue where is_pull = 0 and {REPO_SCOPE} order by `index`",
        (owner, name),
    )
    return [
        DumpIssue(
            db_id=int(db_id),
            number=int(number),
            title=str(title or ""),
            content=str(content or ""),
            closed=bool(closed),
            created_unix=int(created or 0),
            updated_unix=int(updated or 0),
        )
        for db_id, number, title, content, closed, created, updated in rows
    ]


def read_label_names(conn: sqlite3.Connection, owner: str, name: str) -> list[str]:
    """The repository's labels, in name order — the labeller's side of the tracker."""
    return [
        str(label_name)
        for (label_name,) in conn.execute(
            f"select name from label where {REPO_SCOPE} order by name", (owner, name)
        )
    ]


def read_counts_by_issue(
    conn: sqlite3.Connection, sql: str, *params: Any
) -> dict[int, int]:
    return {
        int(issue_id): int(count)
        for issue_id, count in conn.execute(sql, params)
        if issue_id is not None
    }


def read_dump(path: Path, repo: str) -> DumpFacts:
    if not path.is_file():
        die(
            f"tracker-backup: no dump at {path}.\n"
            "  Pass --dump FILE. A dump is environment-local (ADR-0006); see\n"
            "  docs/tracker-backup.md for how one is taken and where it lands."
        )
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        db_member = find_db_member(members)
        if db_member is None:
            listed = ", ".join(sorted(members)[:8]) or "nothing"
            die(
                f"tracker-backup: no database in {path.name}: looked for "
                f"{DB_MEMBER}, found {listed}.\n"
                "  Is this a `gitea dump` archive?"
            )
        with archive.open(db_member) as handle:
            magic = handle.read(len(SQLITE_MAGIC))
        if magic != SQLITE_MAGIC:
            die(
                f"tracker-backup: {db_member} is not a SQLite database.\n"
                "  This audit reads SQLite dumps, which is what it can read with\n"
                "  the standard library alone. An instance on MySQL or PostgreSQL\n"
                "  dumps its schema as SQL text (gitea-db.sql), and counting that\n"
                "  needs a client for that engine."
            )
        sql_member = find_sql_member(members)
        sql_header = read_sql_header(archive, sql_member) if sql_member else None

        with tempfile.TemporaryDirectory(prefix="tracker-backup-") as tmp:
            scratch = Path(tmp)
            archive.extract(db_member, scratch)
            wiki = read_wiki(archive, members, scratch, repo)
            conn = sqlite3.connect(
                f"{(scratch / db_member).as_uri()}?mode=ro", uri=True
            )
            owner, name = split_repo(repo)
            try:
                issues = read_issues(conn, owner, name)
                comments = read_counts_by_issue(
                    conn,
                    "select issue_id, count(*) from comment where type = ?"
                    " group by issue_id",
                    COMMENT_TYPE_COMMENT,
                )
                attachments = read_counts_by_issue(
                    conn, "select issue_id, count(*) from attachment group by issue_id"
                )
                pull_requests = int(
                    conn.execute(
                        f"select count(*) from issue where is_pull = 1 and {REPO_SCOPE}",
                        (owner, name),
                    ).fetchone()[0]
                )
                comment_rows = int(
                    conn.execute("select count(*) from comment").fetchone()[0]
                )
                users = int(conn.execute("select count(*) from user").fetchone()[0])
                label_names = read_label_names(conn, owner, name)
            except sqlite3.OperationalError as exc:
                die(f"tracker-backup: {db_member} is not a Gitea database: {exc}")
            finally:
                conn.close()

    by_number: dict[int, DumpIssue] = {}
    for issue in issues:
        issue.comments = comments.get(issue.db_id, 0)
        issue.attachments = attachments.get(issue.db_id, 0)
        by_number[issue.number] = issue

    return DumpFacts(
        path=path,
        sha256=file_sha256(path),
        size=path.stat().st_size,
        members=len(members),
        db_member=db_member,
        db_format="SQLite 3 (xorm-migrated), opened read-only with the standard library",
        sql_member=sql_member,
        sql_header=sql_header,
        issues=by_number,
        pull_requests=pull_requests,
        comment_rows=comment_rows,
        users=users,
        label_names=label_names,
        wiki=wiki,
    )


# --------------------------------------------------------------------------- #
# Comparing the dump against the source
# --------------------------------------------------------------------------- #


@dataclass
class SourceFacts:
    issues: dict[int, dict[str, Any]]
    wiki_titles: list[str]
    users: int
    label_names: list[str]
    attachments: dict[int, int]  # per sampled issue


@dataclass
class CountRow:
    """One compared quantity: what the dump holds, and what the source holds."""

    label: str
    dump: int
    source: int


@dataclass
class SampleRow:
    number: int
    title: str
    fields: dict[str, bool]
    notes: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return all(self.fields.values())


@dataclass
class Comparison:
    rows: list[CountRow]
    shared: list[int]
    dump_only: list[int]
    source_only: list[int]
    sample: list[SampleRow]
    lost: list[str]
    drift: list[str]

    @property
    def verdict(self) -> str:
        if self.lost:
            return "fail"
        return "drift" if self.drift else "match"

    @property
    def drifted_sample(self) -> list[SampleRow]:
        return [row for row in self.sample if not row.matched]


def read_source(client: Gitea, sample: list[int]) -> SourceFacts:
    issues = client.issues()
    return SourceFacts(
        issues=issues,
        wiki_titles=client.wiki_titles(),
        users=client.user_count(),
        label_names=client.label_names(),
        attachments={
            number: client.issue_attachments(number)
            for number in sample
            if number in issues
        },
    )


def sample_numbers(numbers: list[int], size: int) -> list[int]:
    """The lowest *size* and the highest *size* issue numbers, deduplicated.

    Deterministic on purpose: the same dump and the same size always sample the
    same tickets, so two runs can be read side by side — and both ends of the
    history are covered, not just the first screenful of it.
    """
    ordered = sorted(numbers)
    if size <= 0:
        return []
    return sorted(set(ordered[:size]) | set(ordered[-size:]))


def iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(timespec="seconds")


def source_unix(payload: dict[str, Any], key: str) -> int | None:
    stamp = payload.get(key)
    if not isinstance(stamp, str):
        return None
    try:
        return int(datetime.fromisoformat(stamp).timestamp())
    except ValueError:
        return None


def number_list(numbers: list[int], limit: int = 8) -> str:
    shown = ", ".join(f"#{number}" for number in numbers[:limit])
    return shown + (
        f", … (+{len(numbers) - limit} more)" if len(numbers) > limit else ""
    )


def compare_sample(dump: DumpFacts, source: SourceFacts, number: int) -> SampleRow:
    """One sampled issue, field by field: each field's dump and source values."""
    checked = dump.issues[number]
    payload = source.issues.get(number)
    if payload is None:
        return SampleRow(
            number,
            checked.title,
            dict.fromkeys(SAMPLE_FIELDS, False),
            [f"#{number} is in the dump and not in the source"],
        )

    fields: dict[str, bool] = {}
    notes: list[str] = []

    def check(name: str, dump_value: Any, source_value: Any, shown: str) -> None:
        fields[name] = dump_value == source_value
        if not fields[name]:
            notes.append(shown)

    check(
        "title",
        checked.title,
        payload.get("title"),
        (f"#{number} title: dump {checked.title!r}, source {payload.get('title')!r}"),
    )
    state = str(payload.get("state", ""))
    check(
        "state",
        checked.closed,
        state == "closed",
        (
            f"#{number} state: dump {'closed' if checked.closed else 'open'}, source {state}"
        ),
    )
    body = str(payload.get("body", ""))
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    check(
        "body",
        checked.body_sha256,
        body_sha,
        (
            f"#{number} body: dump {len(checked.content)} chars "
            f"(sha256 {checked.body_sha256[:12]}), source {len(body)} chars "
            f"(sha256 {body_sha[:12]})"
        ),
    )
    created = source_unix(payload, "created_at")
    check(
        "created",
        checked.created_unix,
        created,
        (
            f"#{number} created: dump {iso(checked.created_unix)}, "
            f"source {iso(created) if created else payload.get('created_at')}"
        ),
    )
    updated = source_unix(payload, "updated_at")
    check(
        "updated",
        checked.updated_unix,
        updated,
        (
            f"#{number} updated: dump {iso(checked.updated_unix)}, "
            f"source {iso(updated) if updated else payload.get('updated_at')}"
        ),
    )
    comments = int(payload.get("comments") or 0)
    check(
        "comments",
        checked.comments,
        comments,
        (f"#{number} comments: dump {checked.comments}, source {comments}"),
    )
    assets = source.attachments.get(number, 0)
    check(
        "attachments",
        checked.attachments,
        assets,
        (f"#{number} attachments: dump {checked.attachments}, source {assets}"),
    )
    return SampleRow(number, checked.title, fields, notes)


def compare(dump: DumpFacts, source: SourceFacts, size: int) -> Comparison:
    dump_numbers = sorted(dump.issues)
    source_numbers = sorted(source.issues)
    shared = [number for number in dump_numbers if number in source.issues]
    dump_only = [number for number in dump_numbers if number not in source.issues]
    source_only = [number for number in source_numbers if number not in dump.issues]

    dump_pages = set(dump.wiki.pages)
    source_pages = set(source.wiki_titles)
    dump_labels = set(dump.label_names)
    source_labels = set(source.label_names)
    dump_comments = dump.comments
    source_comments = sum(
        int(source.issues[number].get("comments") or 0) for number in shared
    )
    sample = sample_numbers(dump_numbers, size)
    dump_assets = sum(dump.issues[number].attachments for number in sample)
    source_assets = sum(source.attachments.get(number, 0) for number in sample)
    sample_rows = [compare_sample(dump, source, number) for number in sample]

    lost: list[str] = []
    drift: list[str] = []

    if dump_only:
        lost.append(
            f"{len(dump_only)} issue(s) in the dump are missing from the source: "
            f"{number_list(dump_only)}"
        )
    comment_deficits = [
        number
        for number in shared
        if dump.issues[number].comments
        > int(source.issues[number].get("comments") or 0)
    ]
    if comment_deficits:
        lost.append(
            f"{len(comment_deficits)} issue(s) lost comments: {number_list(comment_deficits)}"
        )
    missing_pages = sorted(dump_pages - source_pages)
    if missing_pages:
        lost.append(
            f"{len(missing_pages)} wiki page(s) in the dump are missing from the "
            f"source: {', '.join(missing_pages[:4])}"
            + (", …" if len(missing_pages) > 4 else "")
        )
    if dump.users > source.users:
        lost.append(
            f"{dump.users - source.users} user account(s) in the dump are missing "
            f"from the source (dump {dump.users}, source {source.users})"
        )
    missing_labels = sorted(dump_labels - source_labels)
    if missing_labels:
        lost.append(
            f"{len(missing_labels)} label(s) in the dump are missing from the "
            f"source: {', '.join(missing_labels[:6])}"
            + (", …" if len(missing_labels) > 6 else "")
        )
    asset_deficits = [
        number
        for number in sample
        if dump.issues[number].attachments > source.attachments.get(number, 0)
    ]
    if asset_deficits:
        lost.append(
            f"{len(asset_deficits)} sampled issue(s) lost attachments: "
            f"{number_list(asset_deficits)}"
        )

    if source_only:
        drift.append(
            f"{len(source_only)} issue(s) exist in the source but not in the dump "
            f"(created after it was taken): {number_list(source_only)}"
        )
    if source_comments > dump_comments:
        drift.append(
            f"{source_comments - dump_comments} comment(s) were added to the dump's "
            "issues after it was taken"
        )
    extra_pages = sorted(source_pages - dump_pages)
    if extra_pages:
        drift.append(
            f"{len(extra_pages)} wiki page(s) exist in the source but not in the "
            f"dump: {', '.join(extra_pages[:4])}"
            + (", …" if len(extra_pages) > 4 else "")
        )
    if source.users > dump.users:
        drift.append(
            f"{source.users - dump.users} user account(s) were created after the "
            "dump was taken"
        )
    extra_labels = sorted(source_labels - dump_labels)
    if extra_labels:
        drift.append(
            f"{len(extra_labels)} label(s) exist in the source but not in the dump: "
            f"{', '.join(extra_labels[:6])}" + (", …" if len(extra_labels) > 6 else "")
        )
    drifted = [row.number for row in sample_rows if not row.matched]
    if drifted:
        drift.append(
            f"{len(drifted)} of {len(sample_rows)} sampled issue(s) differ from the "
            f"source: {number_list(drifted)}"
        )

    rows = [
        CountRow(
            "issues (tickets; pull requests excluded)",
            len(dump_numbers),
            len(source_numbers),
        ),
        CountRow(
            "comments (on the dump's issue numbers)", dump_comments, source_comments
        ),
        CountRow("wiki pages", len(dump_pages), len(source_pages)),
        CountRow("users (accounts on the instance)", dump.users, source.users),
        CountRow(
            "labels (the repository's)", len(dump.label_names), len(source.label_names)
        ),
        CountRow(
            f"attachments (on the {len(sample)} sampled issue(s))",
            dump_assets,
            source_assets,
        ),
    ]
    return Comparison(rows, shared, dump_only, source_only, sample_rows, lost, drift)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def print_dump(facts: DumpFacts) -> None:
    print(f"dump      {facts.path}")
    print(
        f"          {human_bytes(facts.size)}, sha256 {facts.sha256[:16]}…, "
        f"{facts.members} zip members"
    )
    print(f"database  {facts.db_member} — {facts.db_format}")
    if facts.sql_member:
        print(
            f"          {facts.sql_member} carries the same data as SQL text: "
            f"{facts.sql_header or 'header unreadable'}"
        )
    print(
        f"counts    {len(facts.issues)} tickets ({facts.pull_requests} pull request(s) "
        f"excluded), {facts.comments} comments ({facts.comment_rows} comment rows), "
        f"{facts.users} users, {len(facts.label_names)} labels, "
        f"{facts.attachments} attachments"
    )
    if facts.wiki.directory:
        where = f"{facts.wiki.directory} @ {facts.wiki.revision}"
        print(f"wiki      {len(facts.wiki.pages)} pages in {where} ({facts.wiki.ref})")
    else:
        print("wiki      no wiki repository in the dump (0 pages)")


def print_source(source: SourceFacts, url: str, repo: str, origin: str) -> None:
    print(f"source    {url} — repo {repo} ({origin})")
    print(
        f"          {len(source.issues)} tickets, {len(source.wiki_titles)} wiki "
        f"pages, {source.users} users, {len(source.label_names)} labels"
    )


def print_comparison(comparison: Comparison, size: int) -> None:
    width = max(len(row.label) for row in comparison.rows) + 2
    print("counts")
    print(f"  {'entity':<{width}}{'dump':>8}{'source':>8}")
    for row in comparison.rows:
        print(f"  {row.label:<{width}}{row.dump:>8}{row.source:>8}")
    print("issues")
    print(
        f"  in both {len(comparison.shared)} · only in the dump "
        f"{len(comparison.dump_only)} · only in the source "
        f"{len(comparison.source_only)}"
    )
    if comparison.sample:
        print(
            f"sample  {len(comparison.sample)} issues (lowest {size} + highest {size})"
        )
        for row in comparison.sample:
            status = "ok   " if row.matched else "drift"
            title = row.title.replace("\n", " ")
            if len(title) > TITLE_WIDTH:
                title = title[: TITLE_WIDTH - 1] + "…"
            print(f"  #{row.number:<5} {status} {title}")
        for row in comparison.sample:
            for note in row.notes:
                print(f"         {note}")


def print_verdict(comparison: Comparison, facts: DumpFacts, record: Path) -> None:
    if comparison.verdict == "fail":
        print(
            "verdict: FAIL — the source is missing what the dump holds "
            f"({len(comparison.lost)}):"
        )
        for line in comparison.lost:
            print(f"  - {line}")
    elif comparison.verdict == "drift":
        print(
            "verdict: DRIFT — the dump holds every ticket it covers; the source has "
            "moved on since it was taken:"
        )
        for line in comparison.drift:
            print(f"  - {line}")
        print(
            f"         ({len(comparison.shared)} of {len(facts.issues)} dump tickets "
            "are still in the source; a restore of this dump should show no drift)"
        )
    else:
        print(
            f"verdict: MATCH — the dump and the source agree count for count "
            f"({len(comparison.shared)} tickets, {facts.comments} comments, "
            f"{len(facts.wiki.pages)} pages, {len(facts.label_names)} labels), on "
            f"issue numbers, and on all {len(comparison.sample)} sampled issues "
            "field for field"
        )
    print(f"record    {record}")


def record_payload(
    facts: DumpFacts,
    source: SourceFacts | None,
    comparison: Comparison | None,
    url: str | None,
    args: argparse.Namespace,
    stamp: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": "audit_tracker_backup",
        "run_at": stamp,
        "dump": {
            "path": str(facts.path),
            "sha256": facts.sha256,
            "bytes": facts.size,
            "zip_members": facts.members,
            "database": {"member": facts.db_member, "format": facts.db_format},
            "sql_dump": {"member": facts.sql_member, "header": facts.sql_header},
            "counts": {
                "issues": len(facts.issues),
                "pull_requests": facts.pull_requests,
                "comments": facts.comments,
                "comment_rows": facts.comment_rows,
                "users": facts.users,
                "labels": len(facts.label_names),
                "attachments": facts.attachments,
                "wiki_pages": len(facts.wiki.pages),
            },
            "labels": sorted(facts.label_names),
            "wiki": {
                "repository": facts.wiki.directory,
                "ref": facts.wiki.ref,
                "revision": facts.wiki.revision,
                "pages": sorted(facts.wiki.pages),
            },
            "sample_rule": f"lowest {args.sample} + highest {args.sample} issue numbers",
        },
    }
    if comparison is None or source is None:
        payload["source"] = None
        payload["comparison"] = None
        payload["verdict"] = "dump-only"
        return payload
    payload["source"] = {
        "url": url,
        "repo": args.repo,
        "issues": len(source.issues),
        "wiki_pages": len(source.wiki_titles),
        "users": source.users,
        "labels": len(source.label_names),
    }
    payload["comparison"] = {
        "verdict": comparison.verdict,
        "counts": [
            {"entity": row.label, "dump": row.dump, "source": row.source}
            for row in comparison.rows
        ],
        "issues": {
            "shared": len(comparison.shared),
            "dump_only": comparison.dump_only,
            "source_only": comparison.source_only,
        },
        "sample": [
            {
                "number": row.number,
                "match": row.matched,
                "fields": row.fields,
                "notes": row.notes,
            }
            for row in comparison.sample
        ],
        "lost": comparison.lost,
        "drift": comparison.drift,
    }
    payload["verdict"] = comparison.verdict
    return payload


def write_record(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit_tracker_backup.py",
        description=(
            "Check that a `gitea dump` of the tracker holds the tracker: count "
            "what the dump contains — tickets, comments, wiki pages, users, "
            "labels, attachments — and, when the source instance is named, compare "
            "those counts, the names in them and a sample of issues against its "
            "API."
        ),
        epilog=(
            "the procedure\n"
            "  1. take a dump: `gitea dump` inside the instance's container, and\n"
            "     copy the zip out — it is environment-local, never committed;\n"
            "  2. audit it: this script against the dump, and against the live\n"
            "     instance for a comparison (`--url URL`);\n"
            "  3. restore it: `just tracker-restore --take-dump --context NAME\n"
            "     --container NAME --source-url URL` takes a dump where the\n"
            "     instance runs and restores it here with the local Gitea, then\n"
            "     runs this audit against the restored copy, where the verdict\n"
            "     must read MATCH. It refuses to restore across Gitea versions.\n"
            "docs/tracker-backup.md carries the recipe, the evidence and who\n"
            "runs it. Exit status: 0 for MATCH and DRIFT, 1 for FAIL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dump",
        metavar="FILE",
        required=True,
        help="the `gitea dump` zip to audit (environment-local; never defaulted)",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        help=(
            "base URL of the instance to compare the dump against; omit for an "
            f"offline audit, or set ${URL_ENV}"
        ),
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        metavar="OWNER/NAME",
        help=f"repository holding the tickets (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5,
        metavar="N",
        help=(
            "issues to compare field by field: the lowest N and the highest N "
            "numbers (default: 5, so 10 issues; 0 skips the sample)"
        ),
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help=(
            "JSON evidence record to write (default: "
            "<repo>/.local/tracker-backup-audit.json)"
        ),
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    # `just tracker-backup -- --dump FILE` is the usual way to hand flags through
    # a recipe; argparse would read everything after `--` as positional, and this
    # script has no positionals, so drop the separator.
    return parser.parse_args([arg for arg in raw if arg != "--"])


def resolve_url(args: argparse.Namespace) -> str | None:
    """The instance to compare against: ``--url``, ``$CLEAR_RECORD_GITEA_URL``,
    or none at all — an offline audit of the dump alone.

    Deliberately not defaulted, for ``scripts/migrate_tracker.py``'s reason: the
    instance is private, and a convenience default here would be the hostname
    the committed-file guard rejects.
    """
    if args.url:
        return args.url
    return os.environ.get(URL_ENV, "").strip() or None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dump_path = Path(args.dump).expanduser().resolve()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    facts = read_dump(dump_path, args.repo)
    print_dump(facts)

    comparison = None
    source = None
    url = resolve_url(args)
    if url is None:
        print(
            "source    none named: auditing the dump on its own "
            f"(pass --url URL or set ${URL_ENV} to compare)"
        )
    else:
        token, origin = resolve_token()
        try:
            source = read_source(
                Gitea(url, args.repo, token),
                sample_numbers(sorted(facts.issues), args.sample),
            )
        except GiteaError as exc:
            die(
                f"tracker-backup: cannot read {args.repo} at {url} — {exc}\n"
                "  The comparison needs the instance to answer and a token that may\n"
                "  read the repository. The dump was read already; drop --url for an\n"
                "  offline audit of it alone."
            )
        comparison = compare(facts, source, args.sample)
        print_source(source, url, args.repo, origin)
        print_comparison(comparison, args.sample)

    record = (
        Path(args.record).expanduser().resolve()
        if args.record
        else repo_root() / ".local/tracker-backup-audit.json"
    )
    write_record(record, record_payload(facts, source, comparison, url, args, stamp))
    if comparison is None:
        print(
            "verdict: dump-only — no source named; the dump holds what is listed above"
        )
        print(f"record    {record}")
        return 0
    print_verdict(comparison, facts, record)
    return 1 if comparison.verdict == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
