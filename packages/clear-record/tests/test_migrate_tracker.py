"""The tracker importer (``scripts/migrate_tracker.py``) is re-runnable.

The script is billed as one-shot **and** re-runnable: a run that dies half way
is retried with the same command. That promise rests on the provenance marker
each created issue carries, and a marker only proves the *issue* was created —
not that the side effects that follow it landed. Posting the tracker's
``## Comments`` text, closing a ``done`` ticket, and pointing ``Blocked by:`` at
the numbers the manifest settled on all happen after the issue exists, so a
transient error between those two points leaves a ticket that looks complete to
every later run.

So the property under test is not "the marker dedupes" but "a later run finishes
the job": an issue found by its marker is reconciled — the missing comment is
posted once, the state is corrected, the blocked-by line is written — and
nothing is created a second time. The whole thing runs against an in-memory
stand-in for Gitea (no network, no token) over a throwaway tracker in
``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_tracker.py"

COMMENT_TEXT = "Landed on main at `abc1234`; the review gate passed."


@pytest.fixture
def importer() -> types.ModuleType:
    """The script, loaded as a module (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("migrate_tracker", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` looks the class's module up in ``sys.modules`` while the
    # decorator runs, so the module must be registered *before* execution.
    sys.modules["migrate_tracker"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("migrate_tracker", None)


class FakeGitea:
    """In-memory Gitea: just enough surface for an ``--apply`` run.

    Faults are injected by ticket, so a test can lose exactly one side effect —
    the shape of the transient failure the small local instance produces.
    """

    def __init__(self, importer: types.ModuleType) -> None:
        self.importer = importer
        self.label_store: dict[str, dict[str, object]] = {}
        self.issues: dict[int, dict[str, object]] = {}
        self.pages: dict[str, str] = {}
        self.next_number = 1
        self.fail_comment_for: str | None = None
        self.fail_close_for: str | None = None
        self.fail_create_for: str | None = None
        self._next_label_id = 1

    # -- labels ------------------------------------------------------------ #

    def labels(self) -> dict[str, int]:
        return {name: int(label["id"]) for name, label in self.label_store.items()}

    def ensure_label(self, name: str, colour: str, known: dict[str, int]):
        if name in known:
            return known[name], False
        self.label_store[name] = {"id": self._next_label_id, "color": colour}
        known[name] = self._next_label_id
        self._next_label_id += 1
        return known[name], True

    # -- issues ------------------------------------------------------------ #

    def existing_issues(self) -> dict[str, dict[str, object]]:
        found: dict[str, dict[str, object]] = {}
        for issue in self.issues.values():
            match = self.importer.MARKER_RE.search(str(issue["body"]))
            if match:
                found[match.group("key")] = issue
        return found

    def issue_comments(self, number: int) -> list[dict[str, str]]:
        return [{"body": body} for body in self.issues[number]["comments"]]

    def create_issue(self, title: str, body: str, label_ids: list[int]) -> int:
        if self.fail_create_for and self.fail_create_for in body:
            raise self.importer.GiteaError(500, "POST", "/issues", "transient")
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": sorted(label_ids),
            "comments": [],
        }
        return number

    def patch_issue(self, number: int, payload: dict[str, object]) -> None:
        issue = self.issues[number]
        if payload.get("state") == "closed" and self.fail_close_for:
            key = self._key(issue)
            if self.fail_close_for in key:
                raise self.importer.GiteaError(500, "PATCH", "/issues", "transient")
        issue.update(payload)

    def comment(self, number: int, body: str) -> None:
        issue = self.issues[number]
        if self.fail_comment_for and self.fail_comment_for in self._key(issue):
            raise self.importer.GiteaError(500, "POST", "/comments", "transient")
        issue["comments"].append(body)

    # -- wiki -------------------------------------------------------------- #

    def wiki_index(self) -> dict[str, str]:
        return {title: title for title in self.pages}

    def wiki_page(self, sub_url: str) -> str:
        return self.pages[sub_url]

    def create_wiki(self, title: str, content: str) -> None:
        self.pages[title] = content

    def edit_wiki(self, sub_url: str, title: str, content: str) -> None:
        self.pages[sub_url] = content

    # -- test helpers ------------------------------------------------------ #

    def _key(self, issue: dict[str, object]) -> str:
        match = self.importer.MARKER_RE.search(str(issue["body"]))
        return match.group("key") if match else ""

    def by_key(self, key: str) -> dict[str, object]:
        return self.existing_issues()[key]


def make_tracker(root: Path) -> Path:
    """A three-file lane: a closed ticket with comments and a blocked-by line,
    an open one, and a spec (so the wiki path is exercised too)."""
    lane = root / "demo-lane"
    (lane / "issues").mkdir(parents=True)
    (lane / "issues" / "01-first.md").write_text(
        "# 01: The first ticket\n"
        "\n"
        "**Type:** task (refactor). **Status:** done. **Blocked by:** 02.\n"
        "\n"
        "Body of the first ticket.\n"
        "\n"
        "## Comments\n"
        "\n"
        f"{COMMENT_TEXT}\n",
        encoding="utf-8",
    )
    (lane / "issues" / "02-second.md").write_text(
        "# 02: The second ticket\n"
        "\n"
        "**Status:** needs-triage.\n"
        "\n"
        "Body of the second ticket.\n",
        encoding="utf-8",
    )
    (lane / "spec.md").write_text("# Demo lane spec\n\nThe spec.\n", encoding="utf-8")
    return root


def run_apply(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tracker: Path,
    fake: FakeGitea,
    manifest: Path,
) -> int:
    """One ``--apply`` run against *fake*, exactly as the CLI would make it."""
    monkeypatch.setattr(importer, "Gitea", lambda url, repo, token: fake)
    monkeypatch.setattr(importer, "resolve_token", lambda: ("fake-token", "test"))
    return importer.main(
        [
            "--tracker",
            str(tracker),
            "--url",
            "https://gitea.example.test",
            "--manifest",
            str(manifest),
            "--apply",
        ]
    )


def test_a_rerun_repairs_the_side_effects_a_failed_run_lost(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    # The failure that strands a ticket: the issue is created, then the comment
    # post dies (the close patch and the deferred blocked-by patch never run).
    fake.fail_comment_for = "01-first"

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 1
    first = fake.by_key("demo-lane/issues/01-first.md")
    assert first["comments"] == [], "the comment post was lost, as arranged"
    assert first["state"] == "open"
    assert "Blocked by: #" not in str(first["body"])

    # The retry: same command, no injection. Nothing new is created, and every
    # side effect the first run lost is finished.
    fake.fail_comment_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert len(fake.issues) == 2, "no second issue for a ticket that exists"

    first = fake.by_key("demo-lane/issues/01-first.md")
    second = fake.by_key("demo-lane/issues/02-second.md")
    assert first["comments"] == [COMMENT_TEXT]
    assert first["state"] == "closed"
    assert f"Blocked by: #{second['number']}" in str(first["body"])
    assert second["state"] == "open"

    # The retry is itself idempotent: a third run repairs nothing and posts no
    # second copy of the comment.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert len(fake.issues) == 2
    assert first["comments"] == [COMMENT_TEXT]

    # Losing the blocked-by line later is repaired the same way.
    fake.issues[int(first["number"])]["body"] = str(first["body"]).replace(
        f"\n\nBlocked by: #{second['number']}\n", "\n"
    )
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    repaired = fake.by_key("demo-lane/issues/01-first.md")
    assert f"Blocked by: #{second['number']}" in str(repaired["body"])


def test_a_lost_close_is_repaired_without_touching_the_other_issues(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    fake.fail_close_for = "01-first"

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 1
    assert fake.by_key("demo-lane/issues/01-first.md")["state"] == "open"

    fake.fail_close_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert fake.by_key("demo-lane/issues/01-first.md")["state"] == "closed"
    assert fake.by_key("demo-lane/issues/02-second.md")["state"] == "open"
    assert len(fake.issues) == 2


def test_a_blocker_that_never_landed_is_not_reported_as_resolved(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deferred patch must not claim a resolution it did not write.

    Ticket 01 is blocked by 02, so 02's number is unknown when 01 is created and
    01 joins the deferred list. If 02's own creation then fails, there is still
    no number to write: the run has to say so, and leave the body alone.
    """
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    fake.fail_create_for = "02-second"

    assert run_apply(importer, monkeypatch, tracker, fake, tmp_path / "m.json") == 1
    printed = capsys.readouterr().out
    assert "blocked-by left unresolved" in printed
    assert "resolved later" not in printed

    first = fake.by_key("demo-lane/issues/01-first.md")
    assert "Blocked by: #" not in str(first["body"])


def test_a_blocker_supplied_by_a_later_creation_lands_in_the_same_run(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Creation comes first, so the repair sees the number the run just got.

    Ticket 01 is blocked by 02, and an earlier run created 01 but lost 02 — so
    01's ``Blocked by:`` line went with it, and 01 sits in the tracker with no
    line to show it. The next run creates 02: the line has to land with the
    number 02 was just given, not be skipped because ``numbers`` was read before
    02 existed (which exits 0 and leaves the ticket for a third run).
    """
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    fake.fail_create_for = "02-second"

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 1
    assert "Blocked by: #" not in str(
        fake.by_key("demo-lane/issues/01-first.md")["body"]
    ), "01 landed without 02, so it carries no line yet"

    # The first fault-free rerun creates 02 and writes 01's line with it.
    fake.fail_create_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    second = fake.by_key("demo-lane/issues/02-second.md")
    first = fake.by_key("demo-lane/issues/01-first.md")
    assert f"Blocked by: #{second['number']}" in str(first["body"])

    # And the run after it creates nothing and posts no second comment.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert len(fake.issues) == 2
    assert fake.by_key("demo-lane/issues/01-first.md")["comments"] == [COMMENT_TEXT]


def test_a_bare_run_says_where_the_tracker_root_comes_from(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No --tracker and no environment: the tool names the lever, not the path."""
    monkeypatch.delenv(importer.TRACKER_ENV, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        importer.main(["--manifest", str(tmp_path / "manifest.json")])
    assert "--tracker DIR" in str(exit_info.value)
    assert importer.TRACKER_ENV in str(exit_info.value)


def test_a_run_without_a_url_names_the_lever(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No --url and no environment: the tool names the lever, not a hostname."""
    monkeypatch.delenv(importer.URL_ENV, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        importer.main(
            [
                "--tracker",
                str(make_tracker(tmp_path / "tracker")),
                "--manifest",
                str(tmp_path / "manifest.json"),
            ]
        )
    assert "--url URL" in str(exit_info.value)
    assert importer.URL_ENV in str(exit_info.value)
