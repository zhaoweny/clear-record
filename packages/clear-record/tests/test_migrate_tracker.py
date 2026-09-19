"""The tracker importer (``scripts/migrate_tracker.py``) converges; it does not win.

The tracker moved to the instance and the archive stopped being canonical the
moment it did (ADR-0029). A re-run of ``--apply`` may therefore create what is
missing — that is what the provenance marker is for: an issue carrying
``<!-- scratch:<lane>/<relpath> sha=… -->`` was already created, and no run
creates it twice — and it must leave everything else exactly as the tracker has
it. A ticket closed in the tracker stays closed, even where the archive calls the
work unfinished; a wiki page edited in the tracker keeps the edited text.

What a re-run *used* to finish was the tail of an import that died half way:
posting the tracker's ``## Comments`` text, closing a ``done`` ticket, writing
the ``Blocked by:`` line. Making that the default is what let a re-run undo
decisions taken in the tracker, so it now sits behind ``--repair-from-archive``,
which is the explicit pre-cutover act. These tests hold the two halves apart: the
default run leaves a closed ticket and an edited page alone, and the flag
re-imposes the archive when it is asked for.

The whole thing runs against an in-memory stand-in for Gitea (no network, no
token) over a throwaway tracker in ``tmp_path``.
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
MAP_TEXT = "# Demo lane map\n\nNotes, decisions-so-far, fog.\n"


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
    """One lane: a closed ticket with comments and a blocked-by line, an open
    one, and a lane document (so the wiki path is exercised too).

    The lane also has a ``spec.md`` — no longer a wiki page, since the lane's
    spec is an umbrella ticket, so the importer only counts it.
    """
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
    (lane / "map.md").write_text(MAP_TEXT, encoding="utf-8")
    (lane / "spec.md").write_text("# Demo lane spec\n\nThe spec.\n", encoding="utf-8")
    return root


def run_apply(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tracker: Path,
    fake: FakeGitea,
    manifest: Path,
    repair: bool = False,
) -> int:
    """One ``--apply`` run against *fake*, exactly as the CLI would make it."""
    monkeypatch.setattr(importer, "Gitea", lambda url, repo, token: fake)
    monkeypatch.setattr(importer, "resolve_token", lambda: ("fake-token", "test"))
    argv = [
        "--tracker",
        str(tracker),
        "--url",
        "https://gitea.example.test",
        "--manifest",
        str(manifest),
        "--apply",
    ]
    if repair:
        argv.append("--repair-from-archive")
    return importer.main(argv)


def test_the_repair_flag_finishes_the_side_effects_a_failed_run_lost(
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

    # A default retry creates nothing new, and — the tracker now being canonical
    # — leaves the stranded ticket exactly as it found it.
    fake.fail_comment_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert len(fake.issues) == 2, "no second issue for a ticket that exists"
    stranded = fake.by_key("demo-lane/issues/01-first.md")
    assert stranded["comments"] == []
    assert stranded["state"] == "open"
    assert "Blocked by: #" not in str(stranded["body"])

    # The explicit pre-cutover repair is what finishes the job: same command plus
    # the flag, no injection.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    assert len(fake.issues) == 2, "no second issue for a ticket that exists"

    first = fake.by_key("demo-lane/issues/01-first.md")
    second = fake.by_key("demo-lane/issues/02-second.md")
    assert first["comments"] == [COMMENT_TEXT]
    assert first["state"] == "closed"
    assert f"Blocked by: #{second['number']}" in str(first["body"])
    assert second["state"] == "open"

    # The repair is itself idempotent: a further run repairs nothing and posts no
    # second copy of the comment.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    assert len(fake.issues) == 2
    assert first["comments"] == [COMMENT_TEXT]

    # Losing the blocked-by line later is repaired the same way.
    fake.issues[int(first["number"])]["body"] = str(first["body"]).replace(
        f"\n\nBlocked by: #{second['number']}\n", "\n"
    )
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    repaired = fake.by_key("demo-lane/issues/01-first.md")
    assert f"Blocked by: #{second['number']}" in str(repaired["body"])


def test_the_repair_flag_restores_a_close_without_touching_the_other_issues(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    fake.fail_close_for = "01-first"

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 1
    assert fake.by_key("demo-lane/issues/01-first.md")["state"] == "open"

    fake.fail_close_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
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


def test_the_repair_flag_lands_a_blocker_whose_supplier_was_created_later(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Creation comes first, so the repair sees the number the run just got.

    Ticket 01 is blocked by 02, and an earlier run created 01 but lost 02 — so
    01's ``Blocked by:`` line went with it, and 01 sits in the tracker with no
    line to show it. A default run creates 02 and stops there: 01 already exists,
    and the archive does not get to rewrite it. With the pre-cutover repair, the
    line has to land with the number 02 was just given, not be skipped because
    ``numbers`` was read before 02 existed (which exits 0 and leaves the ticket
    for a third run).
    """
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    fake.fail_create_for = "02-second"

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 1
    assert "Blocked by: #" not in str(
        fake.by_key("demo-lane/issues/01-first.md")["body"]
    ), "01 landed without 02, so it carries no line yet"

    # The fault-free default rerun creates 02 and leaves 01 as the tracker has it.
    fake.fail_create_for = None
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    second = fake.by_key("demo-lane/issues/02-second.md")
    assert "Blocked by: #" not in str(
        fake.by_key("demo-lane/issues/01-first.md")["body"]
    ), "an issue that exists is not rewritten by a converging run"

    # The repair, asked for, writes 01's line with the number 02 has now.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    first = fake.by_key("demo-lane/issues/01-first.md")
    assert f"Blocked by: #{second['number']}" in str(first["body"])

    # And the run after it creates nothing and posts no second comment.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    assert len(fake.issues) == 2
    assert fake.by_key("demo-lane/issues/01-first.md")["comments"] == [COMMENT_TEXT]


def test_a_blocker_created_later_in_the_run_lands_without_being_asked_again(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A default run finishes the bodies it creates: one pass imports the lane.

    Both tickets are absent, and 01 is blocked by 02, so 02 has no number yet
    when 01 is created. Coming back to 01's body once 02 exists is the issue's
    own creation being completed, not the archive being re-imposed over the
    tracker — so it stays in the default run, and a fresh import needs no second
    pass to show what blocks what.
    """
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)

    assert run_apply(importer, monkeypatch, tracker, fake, tmp_path / "m.json") == 0
    first = fake.by_key("demo-lane/issues/01-first.md")
    second = fake.by_key("demo-lane/issues/02-second.md")
    assert f"Blocked by: #{second['number']}" in str(first["body"])
    assert "resolved later" in capsys.readouterr().out


def test_a_rerun_does_not_reopen_a_ticket_the_tracker_closed(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tracker decides a ticket's state once the ticket exists.

    Ticket 01's archive copy says ``done`` and ticket 02's says the work is
    unfinished, but the tracker has decided otherwise for both: 01 reopened, 02
    closed. A re-run takes the tracker's word in both directions — the archive
    settles the state of a *new* issue and nothing else — so the closed ticket
    is not reopened and the reopened one is not closed behind the tracker's back.
    """
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    first = fake.by_key("demo-lane/issues/01-first.md")
    second = fake.by_key("demo-lane/issues/02-second.md")
    assert (first["state"], second["state"]) == ("closed", "open")

    fake.patch_issue(int(first["number"]), {"state": "open"})
    fake.patch_issue(int(second["number"]), {"state": "closed"})

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert fake.by_key("demo-lane/issues/01-first.md")["state"] == "open"
    assert fake.by_key("demo-lane/issues/02-second.md")["state"] == "closed"
    assert len(fake.issues) == 2


def test_a_rerun_does_not_overwrite_a_wiki_page_the_tracker_edited(
    importer: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A page that exists is the tracker's: the archive has nothing to add."""
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert fake.pages["demo-lane/map"] == MAP_TEXT, "the missing page was created"

    edited = "# Demo lane map\n\nRewritten in the tracker.\n"
    fake.pages["demo-lane/map"] = edited
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert fake.pages["demo-lane/map"] == edited

    # Converging still covers what the tracker does not have.
    del fake.pages["demo-lane/map"]
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert fake.pages["demo-lane/map"] == MAP_TEXT


def test_the_repair_flag_reimposes_the_archive_and_says_what_it_changed(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pre-cutover act, and the only one that re-imposes the archive."""
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    first = fake.by_key("demo-lane/issues/01-first.md")

    # What the tracker decided after the cutover: 01 reopened with its body
    # rewritten (marker and all — it is the issue's own first line), and the map
    # page edited.
    tracker_body = str(first["body"]).replace(
        "Body of the first ticket.", "Rewritten in the tracker."
    )
    tracker_page = "# Demo lane map\n\nRewritten in the tracker.\n"
    fake.patch_issue(int(first["number"]), {"state": "open", "body": tracker_body})
    fake.pages["demo-lane/map"] = tracker_page

    # A converging run leaves the tracker's work alone.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    left = fake.by_key("demo-lane/issues/01-first.md")
    assert left["state"] == "open"
    assert left["body"] == tracker_body
    assert fake.pages["demo-lane/map"] == tracker_page

    # Asked for, the archive's own side effects come back — the state and the
    # page, not the prose a person wrote — and the run counts what it changed.
    assert run_apply(importer, monkeypatch, tracker, fake, manifest, repair=True) == 0
    repaired = fake.by_key("demo-lane/issues/01-first.md")
    assert repaired["state"] == "closed"
    assert fake.pages["demo-lane/map"] == MAP_TEXT
    printed = capsys.readouterr().out
    assert "1 repaired from the archive" in printed
    assert "1 overwritten from the archive" in printed


def test_the_lane_spec_is_counted_and_written_nowhere(
    importer: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The umbrella-ticket step superseded the spec's wiki page (ADR-0029)."""
    tracker = make_tracker(tmp_path / "tracker")
    fake = FakeGitea(importer)
    manifest = tmp_path / "manifest.json"

    assert (
        importer.main(
            [
                "--tracker",
                str(tracker),
                "--url",
                "https://gitea.example.test",
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )

    assert run_apply(importer, monkeypatch, tracker, fake, manifest) == 0
    assert "demo-lane/map" in fake.pages
    assert "demo-lane/spec" not in fake.pages
    assert not any("Demo lane spec" in text for text in fake.pages.values())
    assert importer.SPEC_NOTE in capsys.readouterr().out, "the report says so"


def test_the_repair_flag_names_the_mode_it_needs(
    importer: types.ModuleType, tmp_path: Path
) -> None:
    """A dry run calls nothing, so it cannot report a repair it never made."""
    with pytest.raises(SystemExit) as exit_info:
        importer.main(
            [
                "--tracker",
                str(make_tracker(tmp_path / "tracker")),
                "--url",
                "https://gitea.example.test",
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--repair-from-archive",
            ]
        )
    assert "--apply" in str(exit_info.value)


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
