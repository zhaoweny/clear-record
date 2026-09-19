"""The tracker backup drill's two scripts: the dump audit, and the restore.

The audit (``scripts/audit_tracker_backup.py``) compares two stores. A dump is
not the tracker because a file exists: it is the tracker only where the tickets,
comments, wiki pages and users in it can be shown to be the tracker's. This audit
is that showing, and its verdict is the thing the restore drill leans on — a
restored instance must read ``MATCH``, while a live instance that has moved on
since the dump was taken must read ``DRIFT`` without being called a failure.

The restore (``scripts/restore_tracker_dump.py``) takes the dump where the
instance runs and reproduces it here, with the local Gitea, on loopback. Its two
deciding answers are tested here too: the version it refuses to restore across,
and the address it binds.

These tests run the audit against a synthetic dump (a real SQLite database and a
real git wiki repository inside a zip, both built in ``tmp_path``) and an
in-memory stand-in for the instance's API, so no network, no token and no Docker
are involved; the binary the drill would run is stood in for by a shell script.

The dump's rows are deliberately numbered apart from its issue numbers — row 7
carries ticket 1 — because comments and attachments join to the *row*, and a
mix-up there would be invisible in a dump where the two happen to coincide.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import types
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_tracker_backup.py"

# A timezone that is not UTC, so a comparison that forgets the offset fails here
# rather than against the live instance.
TZ = timezone(timedelta(hours=8))

WIKI_REPO = "repos/zhaow/clear-record.wiki.git"

# The repository the labels hang off, as the dump's own tables carry it: `--repo
# zhaow/clear-record` is resolved there, so the fixture has to look like them for
# the audit to find its tickets at all.
REPO_ROW_ID = 7
OWNER_ROW_ID = 3
OWNER = "zhaow"
NAME = "clear-record"

# The tracker's own labels, the ones the importer created (migrate_tracker.py).
LABELS = ["from/scratch", "lane/demo", "type/task"]
WIKI_PAGES = {
    # Gitea's escapes: a subpage's slash becomes %2F, and a name it may read as a
    # date carries a `.-` before the extension.
    "console-ia%2Fmap.-.md": "# Map\n",
    "mrliu%2Fsession.md": "# Session\n",
}


@pytest.fixture
def audit() -> types.ModuleType:
    """The script, loaded as a module (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("audit_tracker_backup", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` looks the class's module up in ``sys.modules`` while the
    # decorator runs, so the module must be registered *before* execution.
    sys.modules["audit_tracker_backup"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("audit_tracker_backup", None)


# --------------------------------------------------------------------------- #
# A dump, built the way the instance's would be
# --------------------------------------------------------------------------- #


def build_dump(
    root: Path,
    *,
    tickets: list[tuple[int, str, str, bool, int]],
    comments: list[tuple[int, int]] = (),
    attachments: list[int] = (),
    users: int = 2,
    labels: list[str] | None = None,
    wiki: bool = False,
) -> Path:
    """Write a zip holding a SQLite database and, optionally, a wiki repository.

    ``tickets`` are ``(number, title, body, closed, updated_unix)``; a ticket's
    row id is its number plus 6, so a join on the wrong column is visible.
    ``comments`` are ``(issue_row_id, comment_type)`` pairs. ``created_unix`` is
    the updated one minus 60, which is enough to tell the two apart. ``labels``
    defaults to the tracker's own three.
    """
    db = root / "gitea.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table issue (
            id integer primary key, repo_id integer, `index` integer, name text,
            content text, is_closed integer, is_pull integer, created_unix integer,
            updated_unix integer
        );
        create table comment (id integer primary key, issue_id integer, type integer);
        create table attachment (id integer primary key, issue_id integer);
        create table user (id integer primary key, lower_name text);
        create table repository (
            id integer primary key, owner_id integer, lower_name text
        );
        create table label (id integer primary key, repo_id integer, name text);
        """
    )
    conn.execute("insert into user values (?, ?)", (OWNER_ROW_ID, OWNER))
    conn.execute(
        "insert into repository values (?, ?, ?)", (REPO_ROW_ID, OWNER_ROW_ID, NAME)
    )
    for number, title, body, closed, updated in tickets:
        conn.execute(
            "insert into issue values (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                number + 6,
                REPO_ROW_ID,
                number,
                title,
                body,
                int(closed),
                updated - 60,
                updated,
            ),
        )
    for index, (issue_id, kind) in enumerate(comments):
        conn.execute(
            "insert into comment values (?, ?, ?)", (index + 1, issue_id, kind)
        )
    for index, issue_id in enumerate(attachments):
        conn.execute("insert into attachment values (?, ?)", (index + 1, issue_id))
    conn.executemany(
        "insert into user values (?, ?)",
        # ``users`` counts every account, the owner row included.
        [(n, f"user{n}") for n in range(1, users) if n != OWNER_ROW_ID],
    )
    conn.executemany(
        "insert into label values (?, ?, ?)",
        [
            (index + 1, REPO_ROW_ID, label)
            for index, label in enumerate(LABELS if labels is None else labels)
        ],
    )
    conn.commit()
    conn.close()

    dump = root / "gitea-dump-test.zip"
    with zipfile.ZipFile(dump, "w") as archive:
        archive.write(db, "data/gitea.db")
        archive.writestr("data/conf/app.ini", "[database]\nDB_TYPE = sqlite3\n")
        archive.writestr(
            "gitea-db.sql", "/*Generated by xorm, from sqlite3 to sqlite3*/\n"
        )
        if wiki:
            write_wiki(archive, root)
    return dump


def write_wiki(archive: zipfile.ZipFile, root: Path) -> None:
    """Commit the pages into a git repository and store it the way a dump does."""
    work = root / "wiki-work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", work], check=True)
    for name, text in WIKI_PAGES.items():
        (work / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", work, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            work,
            "-c",
            "user.name=Audit Test",
            "-c",
            "user.email=audit@example.test",
            "commit",
            "-q",
            "-m",
            "pages",
        ],
        check=True,
    )
    git_dir = work / ".git"
    for path in sorted(git_dir.rglob("*")):
        if path.is_file():
            archive.write(path, f"{WIKI_REPO}/{path.relative_to(git_dir).as_posix()}")


# --------------------------------------------------------------------------- #
# The instance's API, stood in for
# --------------------------------------------------------------------------- #


class FakeGitea:
    """In-memory Gitea: the four reads the audit makes, and nothing else."""

    def __init__(
        self,
        issues: list[dict[str, object]],
        *,
        wiki_titles: list[str] | None = None,
        users: int = 2,
        labels: list[str] | None = None,
        assets: dict[int, int] | None = None,
    ) -> None:
        self.store = {int(issue["number"]): issue for issue in issues}
        self.wiki = list(wiki_titles or [])
        self.users = users
        self.labels = list(LABELS if labels is None else labels)
        self.assets = dict(assets or {})

    def issues(self) -> dict[int, dict[str, object]]:
        return dict(self.store)

    def wiki_titles(self) -> list[str]:
        return list(self.wiki)

    def user_count(self) -> int:
        return self.users

    def label_names(self) -> list[str]:
        return list(self.labels)

    def issue_attachments(self, number: int) -> int:
        return self.assets.get(number, 0)


def api_issue(
    number: int,
    title: str,
    body: str,
    *,
    closed: bool = False,
    created: int = 1_700_000_000,
    updated: int | None = None,
    comments: int = 0,
) -> dict[str, object]:
    """One issue as the API returns it, timestamps in a non-UTC offset."""
    stamps = {"created_at": created, "updated_at": updated or created}
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "closed" if closed else "open",
        "comments": comments,
        **{
            key: datetime.fromtimestamp(unix, TZ).isoformat()
            for key, unix in stamps.items()
        },
    }


def run_audit(
    audit: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    dump: Path,
    fake: FakeGitea,
    record: Path,
    *extra: str,
) -> int:
    """One audit run against *fake*, exactly as the CLI would make it."""
    monkeypatch.setattr(audit, "Gitea", lambda url, repo, token: fake)
    monkeypatch.setattr(audit, "resolve_token", lambda: ("fake-token", "test"))
    return audit.main(
        [
            "--dump",
            str(dump),
            "--url",
            "https://gitea.example.test",
            "--record",
            str(record),
            *extra,
        ]
    )


def verdict_of(record: Path) -> str:
    return str(json.loads(record.read_text(encoding="utf-8"))["verdict"])


# --------------------------------------------------------------------------- #
# The tests
# --------------------------------------------------------------------------- #


def test_a_dump_the_source_still_agrees_with_reads_match(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Counts, the sample and the wiki all agree — the verdict is MATCH.

    The comment rows include a system event (type 3), which is not conversation:
    the comment count must be the type-0 rows only, or this reads as a dump that
    lost 10 comments.
    """
    dump = build_dump(
        tmp_path,
        tickets=[
            (1, "01: First", "# 01: First\n\nBody one.\n", True, 1_700_000_100),
            (2, "02: Second", "# 02: Second\n\nBody two.\n", False, 1_700_000_200),
        ],
        comments=[(7, 0), (7, 0), (7, 3)],
        users=2,
    )
    fake = FakeGitea(
        [
            api_issue(
                1,
                "01: First",
                "# 01: First\n\nBody one.\n",
                closed=True,
                created=1_700_000_040,
                updated=1_700_000_100,
                comments=2,
            ),
            api_issue(
                2,
                "02: Second",
                "# 02: Second\n\nBody two.\n",
                created=1_700_000_140,
                updated=1_700_000_200,
            ),
        ]
    )
    record = tmp_path / "record.json"

    assert run_audit(audit, monkeypatch, dump, fake, record) == 0
    assert verdict_of(record) == "match"

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["dump"]["counts"]["comments"] == 2, "only type-0 rows are comments"
    assert payload["dump"]["counts"]["comment_rows"] == 3
    assert payload["dump"]["counts"]["issues"] == 2
    assert payload["dump"]["counts"]["labels"] == 3
    assert payload["dump"]["labels"] == LABELS
    label_row = next(
        row
        for row in payload["comparison"]["counts"]
        if row["entity"] == "labels (the repository's)"
    )
    assert label_row == {
        "entity": "labels (the repository's)",
        "dump": 3,
        "source": 3,
    }
    # Both tickets are sampled (the lowest one and the highest one), field by
    # field, and every field agrees.
    assert [row["number"] for row in payload["comparison"]["sample"]] == [1, 2]
    assert all(row["match"] for row in payload["comparison"]["sample"])
    assert payload["comparison"]["lost"] == []
    assert payload["comparison"]["drift"] == []


def test_a_source_that_moved_on_after_the_dump_drifts_without_failing(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live instance keeps working: new tickets and comments are DRIFT, not FAIL."""
    dump = build_dump(
        tmp_path,
        tickets=[(1, "01: First", "# 01: First\n", True, 1_700_000_100)],
        comments=[(7, 0)],
    )
    fake = FakeGitea(
        [
            api_issue(
                1,
                "01: First",
                "# 01: First\n",
                closed=True,
                created=1_700_000_040,
                updated=1_700_000_100,
                comments=2,
            ),
            api_issue(9, "09: Later", "# 09: Later\n", created=1_700_000_900),
        ]
    )
    record = tmp_path / "record.json"

    assert run_audit(audit, monkeypatch, dump, fake, record) == 0
    assert verdict_of(record) == "drift"

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["comparison"]["issues"]["dump_only"] == []
    assert payload["comparison"]["issues"]["source_only"] == [9]
    assert any("#9" in line for line in payload["comparison"]["drift"])
    assert any("comment" in line for line in payload["comparison"]["drift"])


def test_a_ticket_the_dump_holds_and_the_source_lost_fails(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The verdict the drill needs: history in the dump, missing from the source."""
    dump = build_dump(
        tmp_path,
        tickets=[
            (1, "01: First", "# 01: First\n", True, 1_700_000_100),
            (2, "02: Second", "# 02: Second\n", False, 1_700_000_200),
        ],
    )
    fake = FakeGitea(
        [api_issue(1, "01: First", "# 01: First\n", closed=True, updated=1_700_000_100)]
    )
    record = tmp_path / "record.json"

    assert run_audit(audit, monkeypatch, dump, fake, record) == 1
    assert verdict_of(record) == "fail"

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["comparison"]["issues"]["dump_only"] == [2]
    assert any(
        "missing from the source" in line for line in payload["comparison"]["lost"]
    )


def test_a_sampled_field_that_differs_is_named(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A body edited after the dump is drift, and the note names the field."""
    dump = build_dump(
        tmp_path,
        tickets=[(1, "01: First", "# 01: First\n\nBody one.\n", False, 1_700_000_100)],
    )
    fake = FakeGitea(
        [
            api_issue(
                1,
                "01: First",
                "# 01: First\n\nBody one, edited.\n",
                created=1_700_000_040,
                updated=1_700_000_500,
            )
        ]
    )
    record = tmp_path / "record.json"

    assert run_audit(audit, monkeypatch, dump, fake, record, "--sample", "1") == 0
    assert verdict_of(record) == "drift"

    payload = json.loads(record.read_text(encoding="utf-8"))
    row = payload["comparison"]["sample"][0]
    assert row["number"] == 1
    assert row["fields"]["body"] is False
    assert row["fields"]["updated"] is False
    assert row["fields"]["title"] is True
    assert any("body" in note for note in row["notes"])
    assert any("updated" in note for note in row["notes"])


def test_the_sample_takes_both_ends_of_the_history(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lowest N plus highest N, deduplicated — a deterministic sample."""
    dump = build_dump(
        tmp_path,
        tickets=[
            (1, "01", "# 01\n", False, 1_700_000_001),
            (2, "02", "# 02\n", False, 1_700_000_002),
            (3, "03", "# 03\n", False, 1_700_000_003),
        ],
    )
    fake = FakeGitea(
        [
            api_issue(
                number,
                f"{number:02d}",
                f"# {number:02d}\n",
                created=1_700_000_000 + number - 60,
                updated=1_700_000_000 + number,
            )
            for number in (1, 2, 3)
        ]
    )
    record = tmp_path / "record.json"

    # A sample larger than the dump is every ticket, once.
    assert run_audit(audit, monkeypatch, dump, fake, record, "--sample", "5") == 0
    sample = json.loads(record.read_text(encoding="utf-8"))["comparison"]["sample"]
    assert [row["number"] for row in sample] == [1, 2, 3]

    # And the default sample of a three-ticket dump is still every ticket.
    assert run_audit(audit, monkeypatch, dump, fake, record) == 0
    sample = json.loads(record.read_text(encoding="utf-8"))["comparison"]["sample"]
    assert [row["number"] for row in sample] == [1, 2, 3]


def test_a_source_that_lost_a_label_fails(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restore that dropped a label is a failed restore, not a quiet MATCH.

    Labels are how the tracker says which lane a ticket belongs to and what its
    state was, and they are not in any issue body or comment: a restore can lose
    every one of them and still return every ticket. So the audit compares them
    by name, and a name the dump holds and the instance does not is a lost thing.
    """
    dump = build_dump(
        tmp_path,
        tickets=[(1, "01", "# 01\n", False, 1_700_000_001)],
        labels=["from/scratch", "lane/demo", "type/task"],
    )
    issue = api_issue(1, "01", "# 01\n", created=1_699_999_941, updated=1_700_000_001)
    record = tmp_path / "record.json"

    whole = FakeGitea([issue], labels=["from/scratch", "lane/demo", "type/task"])
    assert run_audit(audit, monkeypatch, dump, whole, record) == 0
    assert verdict_of(record) == "match"

    short = FakeGitea([issue], labels=["from/scratch", "lane/demo"])
    assert run_audit(audit, monkeypatch, dump, short, record) == 1
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["comparison"]["verdict"] == "fail"
    assert any(
        "label" in line and "type/task" in line
        for line in payload["comparison"]["lost"]
    )

    # And a label the instance grew after the dump is drift, not loss.
    grown = FakeGitea(
        [issue], labels=["from/scratch", "lane/demo", "type/task", "lane/later"]
    )
    assert run_audit(audit, monkeypatch, dump, grown, record) == 0
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["comparison"]["verdict"] == "drift"
    assert any("lane/later" in line for line in payload["comparison"]["drift"])


def test_the_wiki_pages_come_from_the_dumps_git_repository(
    audit: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pages are the tree at the wiki repository's HEAD, decoded to their titles."""
    dump = build_dump(
        tmp_path, tickets=[(1, "01", "# 01\n", False, 1_700_000_001)], wiki=True
    )
    issue = api_issue(1, "01", "# 01\n", created=1_699_999_941, updated=1_700_000_001)
    record = tmp_path / "record.json"

    both = FakeGitea([issue], wiki_titles=["console-ia/map", "mrliu/session"])
    assert run_audit(audit, monkeypatch, dump, both, record) == 0
    assert verdict_of(record) == "match"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["dump"]["wiki"]["pages"] == ["console-ia/map", "mrliu/session"]
    assert payload["dump"]["wiki"]["ref"] == "main"

    # A page the dump holds and the restored wiki does not is a failed restore.
    missing = FakeGitea([issue], wiki_titles=["mrliu/session"])
    assert run_audit(audit, monkeypatch, dump, missing, record) == 1
    assert verdict_of(record) == "fail"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert any("console-ia/map" in line for line in payload["comparison"]["lost"])


# --------------------------------------------------------------------------- #
# The restore drill (``scripts/restore_tracker_dump.py``)
#
# The drill's two answers that decide whether a restore happens at all, and
# whether it is safe to stand up: the version it refuses to cross, and the
# address it lets the copy be reachable at. Neither needs Docker, a binary or a
# network — the binary is stood in for by a shell script that answers the two
# calls the drill makes.
# --------------------------------------------------------------------------- #

RESTORE_PATH = REPO_ROOT / "scripts" / "restore_tracker_dump.py"


@pytest.fixture
def restore() -> types.ModuleType:
    """The restore script, loaded as a module (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("restore_tracker_dump", RESTORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["restore_tracker_dump"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("restore_tracker_dump", None)


def stub_gitea(root: Path, version: str, secret: str = "stub-secret") -> Path:
    """A stand-in for the local ``gitea`` binary: version, and a fresh secret.

    Those are the only two things the drill asks a binary, so a shell script
    covers it — and the version it reports is what the refusal turns on.
    """
    path = root / "gitea"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "gitea version {version} built with go1.27.1 : bindata, sqlite";;\n'
        f'  generate) echo "{secret}-$3";;\n'
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class FakeProcess:
    """A started instance, for the tests that must not start one."""

    def __init__(self) -> None:
        self.returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def test_a_version_mismatch_is_refused_before_anything_is_restored(
    restore: types.ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The source's release is not on this machine: refuse, and restore nothing.

    A restore across versions forwards the schema, so it answers a different
    question than the drill asks. The refusal has to name both releases, or the
    reader cannot know which one to install — and it has to come before the dump
    is extracted, so a refused run leaves no half-restored tree behind.
    """
    dump = build_dump(tmp_path, tickets=[(1, "one", "body", False, 1_700_000_000)])
    binary = stub_gitea(tmp_path, "1.27.3")
    scratch = tmp_path / "scratch"

    with pytest.raises(SystemExit) as excinfo:
        restore.main(
            [
                "--dump",
                str(dump),
                "--gitea",
                str(binary),
                "--source-version",
                "1.28.0",
                "--scratch",
                str(scratch),
            ]
        )

    assert excinfo.value.code == 2
    refusal = capsys.readouterr().err
    assert "version mismatch" in refusal
    assert "1.28.0" in refusal and "1.27.3" in refusal
    assert not scratch.exists()


def test_the_generated_configuration_binds_loopback_only(
    restore: types.ModuleType, tmp_path: Path
) -> None:
    """The copy is reachable from this machine and nowhere else — and says so.

    The configuration is generated, not the instance's: it names no address of
    the instance's, and it enables nothing that could reach outward. That is what
    makes standing a copy of a private instance up for an audit safe, and the
    instance's own log has to bear it out.
    """
    binary = stub_gitea(tmp_path, "1.27.3", secret="stub-secret")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    config = restore.write_config(scratch, 41234, str(binary))
    text = config.read_text(encoding="utf-8")

    assert config == scratch / "custom/conf/app.ini"
    assert "HTTP_ADDR = 127.0.0.1\n" in text
    assert "HTTP_PORT = 41234\n" in text
    assert "ROOT_URL = http://127.0.0.1:41234/\n" in text
    assert "0.0.0.0" not in text
    assert "OFFLINE_MODE = true" in text
    assert "SECRET_KEY = stub-secret-SECRET_KEY" in text
    assert "[mailer]\nENABLED = false" in text
    assert "[federation]\nENABLED = false" in text

    log = tmp_path / "gitea.log"
    log.write_text(
        f"* WorkPath: {scratch}\n"
        f"* CustomPath: {scratch}/custom\n"
        f"Creating new Local Storage at {scratch}/data/avatars\n"
        "Listen: http://127.0.0.1:41234\n",
        encoding="utf-8",
    )
    named, used = restore.verify_paths(scratch, config, log, 41234)
    assert named["database.PATH"] == f"{scratch}/data/gitea.db"
    assert used["WorkPath"] == str(scratch)
    assert used["Local Storage (1)"] == f"{scratch}/data/avatars"

    # A copy that listens anywhere but loopback is refused, whatever the config
    # file says: this is the address the instance itself reported.
    log.write_text(f"* WorkPath: {scratch}\nListen: http://0.0.0.0:41234\n")
    with pytest.raises(SystemExit) as excinfo:
        restore.verify_paths(scratch, config, log, 41234)
    assert excinfo.value.code == 2


def test_a_restored_path_outside_the_scratch_root_is_refused(
    restore: types.ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The false green this drill exists to remove: an instance on no data.

    The dump's own configuration points at the container's absolute paths, which
    do not exist here. An instance started against those comes up empty, and an
    audit of it compares the live counts against nothing — so a path the instance
    reports outside the scratch root is a refusal, not a warning.
    """
    binary = stub_gitea(tmp_path, "1.27.3")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = restore.write_config(scratch, 41234, str(binary))

    log = tmp_path / "gitea.log"
    log.write_text(
        "* WorkPath: /data/gitea\n"
        "Creating new Local Storage at /data/gitea/avatars\n"
        "Listen: http://127.0.0.1:41234\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        restore.verify_paths(scratch, config, log, 41234)

    assert excinfo.value.code == 2
    refusal = capsys.readouterr().err
    assert "/data/gitea" in refusal
    assert str(scratch) in refusal


def test_a_database_that_is_not_the_dumps_is_refused(
    restore: types.ModuleType,
    audit: types.ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The instance must read the database the dump holds, not merely a database.

    The audit reads the dump's database out of the zip; the instance reads the
    file the generated config names. If those are not the same bytes the drill is
    comparing a dump against something else, and its counts mean nothing — so the
    digest is checked and the refusal names both.
    """
    dump = build_dump(tmp_path, tickets=[(1, "one", "body", False, 1_700_000_000)])
    facts = audit.read_dump(dump, "zhaow/clear-record")
    scratch = tmp_path / "scratch"
    extracted = scratch / facts.db_member
    extracted.parent.mkdir(parents=True)
    with zipfile.ZipFile(dump) as archive:
        original = archive.read(facts.db_member)
    extracted.write_bytes(original)

    # The dump's own database: accepted, and what it returns is its digest.
    assert restore.verify_database(dump, scratch, facts)
    capsys.readouterr()

    # A database the dump does not hold — one byte is enough.
    extracted.write_bytes(original + b"\n")
    with pytest.raises(SystemExit) as excinfo:
        restore.verify_database(dump, scratch, facts)

    assert excinfo.value.code == 2
    refusal = capsys.readouterr().err
    assert "not the dump's" in refusal
    assert facts.db_member in refusal


def test_an_audit_that_cannot_run_still_leaves_the_drill_record(
    restore: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure after the restore started must not take the evidence with it.

    The likeliest shape is the named-source comparison: the restored copy matches
    the dump, and then the source cannot be read. The drill has to stop the
    instance it started, keep the extracted data to look at, and leave
    ``drill.json`` saying what did not happen — a record that exists and names the
    failure is worth more than no record at all, which is exactly the case it is
    for.
    """
    dump = build_dump(tmp_path, tickets=[(1, "one", "body", False, 1_700_000_000)])
    binary = stub_gitea(tmp_path, "1.27.3")
    scratch = tmp_path / "scratch"
    record = scratch / "drill.json"

    monkeypatch.setattr(restore, "start_instance", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(restore, "wait_for_instance", lambda *a, **k: "status=pass")
    monkeypatch.setattr(restore, "verify_paths", lambda *a, **k: ({}, {}))
    # An audit that writes no record and reports the setup failure: 2 is what
    # `audit_tracker_backup.py` exits with when it cannot read the source.
    monkeypatch.setattr(restore, "run_audit", lambda *a, **k: 2)

    code = restore.main(
        [
            "--dump",
            str(dump),
            "--gitea",
            str(binary),
            "--scratch",
            str(scratch),
            "--source-version",
            "1.27.3",
            "--source-url",
            "https://source.invalid",
            "--port",
            "41234",
        ]
    )

    assert code == 1
    assert record.is_file(), "a started restore owes a drill record"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert "wrote no record" in str(payload["failure"])
    assert payload["verdict"] == {"restored": "not run", "source": "not run"}
    assert payload["cleanup"] == {"instance_stopped": True, "payload_removed": False}
    assert (scratch / "data").is_dir(), "a failure keeps the data to look at"
    assert "FAIL" in capsys.readouterr().out


def test_a_dry_run_runs_nothing_at_all(
    restore: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` is the reader's door in: no Docker, no binary, no extraction.

    It is what a reader with no Docker has, so it must survive having no Docker
    and print both reader commands — the one with the daemon, and the one with a
    dump already in hand.
    """

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a dry run must run no command at all")

    monkeypatch.setattr(restore.subprocess, "run", refuse)
    scratch = tmp_path / "scratch"

    code = restore.main(
        [
            "--take-dump",
            "--container",
            "gitea",
            "--context",
            "example",
            "--scratch",
            str(scratch),
            "--dry-run",
        ]
    )
    printed = capsys.readouterr().out

    assert code == 0
    assert not scratch.exists()
    assert "--dump FILE" in printed  # the reader with no Docker at all
    assert "gitea dump" in printed  # the reader with the daemon
