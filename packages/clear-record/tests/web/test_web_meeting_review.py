"""The console's meeting review surface: transcript, artifacts, agent drafts.

The meeting review is server-rendered over the same service seam the API and MCP
use, so these tests drive it with a temp registry and a temp meeting workspace. A
draft is **written the way a harness writes it** — through
:meth:`~clear_record.service.MeetingAgent.write` — because the console no longer
launches anything itself; what is asserted here is the human's half: the draft is
reviewable in place, accepting it shows the promoted result (and, for minutes,
the project page), and rejecting keeps the chain as history.
"""

from __future__ import annotations

from pathlib import Path

from _console import signed_in
from clear_record.core import RecordDocument, Segment, write_json
from clear_record.pipeline.workspace import Workspace, publish_run
from clear_record.service import (
    AGENT_DIRNAME,
    CONSOLE,
    MCP,
    MeetingAgent,
    Registry,
    RunManager,
)
from clear_record.web.app import create_app
from fastapi.testclient import TestClient

_ANSWERS: dict[str, dict] = {
    "glossary_collection": {
        "terms": [
            {
                "term": "Falcon",
                "reading": "FAL-kun",
                "aliases": ["falcon"],
                "definition": "a tracked object",
                "evidence": "the falcon is up",
            }
        ]
    },
    "transcript_check": {
        "revision": "00:00:03.000 [mic] the Falcon is up",
        "changes": [{"before": "falcon", "after": "Falcon", "reason": "glossary term"}],
    },
    "minutes": {
        "meeting": "Kickoff",
        "project": "Ops",
        "attendees": ["Ada"],
        "decisions": ["ship it"],
        "actions": ["Ada writes the docs"],
        "body": "# Kickoff\n\nWe shipped it.",
    },
}


class SimpleConsole:
    def __init__(self, *, client, registry, meeting, workspace) -> None:
        self.client = client
        self.registry = registry
        self.meeting = meeting
        self.workspace = workspace

    def write(
        self,
        kind: str,
        actor: str = CONSOLE,
        value: dict | None = None,
        draft_id: str | None = None,
    ):
        """Write one draft as ``actor``: the console by default, or a harness.

        A version's recorded author is the actor its transport supplies
        (ADR-0033), so a test that wants a second writer passes another actor —
        there is no author for a caller to declare.
        """
        return MeetingAgent(self.registry, self.meeting).write(
            kind,
            value if value is not None else _ANSWERS[kind],
            actor=actor,
            draft_id=draft_id,
        )

    def drafts(self) -> list[dict]:
        res = self.client.get(f"/api/meetings/{self.meeting.id}/agent")
        assert res.status_code == 200
        return res.json()["drafts"]


def _console(tmp_path: Path) -> SimpleConsole:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor="console",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(
                Segment(start=3.0, end=4.0, text="the falcon is up", source="a"),
            ),
        ),
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor="console",
    )
    app = create_app(registry, RunManager(registry))
    return SimpleConsole(
        client=signed_in(TestClient(app)),
        registry=registry,
        meeting=meeting,
        workspace=workspace,
    )


def test_the_meeting_view_shows_the_transcript_and_artifacts(tmp_path: Path) -> None:
    console = _console(tmp_path)
    console.registry.add_artifact(
        console.meeting.id,
        kind="record",
        path=str(console.workspace / "record.json"),
        produced_by="pipeline",
        review_state="final",
        actor="console",
    )

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert page.status_code == 200
    assert "the falcon is up" in page.text
    assert "record.json" in page.text
    assert "pipeline" in page.text  # the artifact's provenance
    # The panel explains where drafts come from; it launches nothing itself.
    assert "Your harness writes these over MCP" in page.text
    assert "/agent/glossary_collection" not in page.text


def test_the_project_view_links_to_the_meeting_review(tmp_path: Path) -> None:
    console = _console(tmp_path)
    page = console.client.get("/ui/projects/ops/meetings")
    assert "Review" in page.text
    assert 'href="/projects/ops/meetings/kickoff"' in page.text


def test_each_kind_lands_as_a_reviewable_draft_with_its_author(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)

    for kind in _ANSWERS:
        console.write(kind)

    page = console.client.get("/ui/projects/ops/meetings/kickoff")
    for kind in _ANSWERS:
        assert kind in page.text
    # The version records the actor the console supplied — the transport's word,
    # never a name a caller declared (ADR-0033).
    assert CONSOLE in page.text
    assert "Accept" in page.text and "Reject" in page.text
    assert sorted(draft["kind"] for draft in console.drafts()) == sorted(_ANSWERS)


def test_a_second_version_is_shown_on_the_chain(tmp_path: Path) -> None:
    console = _console(tmp_path)
    first = console.write("minutes")
    console.write(
        "minutes",
        actor=MCP,
        value=_ANSWERS["minutes"],
        draft_id=first.draft_id,
    )

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert "2 versions" in page.text
    # One chain, two writers: each version records the actor that wrote it.
    assert CONSOLE in page.text and MCP in page.text
    assert first.draft_id in page.text


def test_accepting_glossary_draft_adds_candidate_terms(tmp_path: Path) -> None:
    console = _console(tmp_path)
    draft = console.write("glossary_collection")

    page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{draft.draft_id}/accept",
        data={"version": draft.version},
    )

    assert page.status_code == 200
    assert "1 term added as a candidate" in page.text
    assert [
        (term.term, term.status) for term in console.registry.list_terms("ops")
    ] == [("Falcon", "candidate")]


def test_accepting_minutes_shows_them_per_meeting_and_across_the_project(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)
    draft = console.write("minutes")

    meeting_page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{draft.draft_id}/accept",
        data={"version": draft.version},
    )
    assert "We shipped it." in meeting_page.text
    assert "Minutes recorded for this meeting." in meeting_page.text

    project_page = console.client.get("/ui/projects/ops")
    assert "table-minutes" in project_page.text
    assert "Kickoff" in project_page.text


def test_rejecting_a_draft_keeps_it_and_promotes_nothing(tmp_path: Path) -> None:
    console = _console(tmp_path)
    draft = console.write("glossary_collection")

    page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{draft.draft_id}/reject",
        data={"version": draft.version},
    )

    assert page.status_code == 200
    assert "rejected" in page.text
    assert console.registry.list_terms("ops") == []
    assert console.drafts()[0]["review_state"] == "rejected"


def test_the_panel_names_the_0_2_drafts_it_does_not_read(tmp_path: Path) -> None:
    """A 0.2 run directory is stated, not silently invisible."""
    console = _console(tmp_path)
    legacy = console.workspace / AGENT_DIRNAME / "minutes-abc123"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text("{}", encoding="utf-8")

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert "written before 0.3" in page.text
    assert "still on disk" in page.text


def test_the_panel_says_nothing_about_0_2_drafts_when_there_are_none(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert "written before 0.3" not in page.text


def test_the_console_posts_the_version_and_a_stale_one_is_refused(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)
    first = console.write("minutes")
    console.write(
        "minutes",
        actor=MCP,
        value=_ANSWERS["minutes"] | {"body": "# Two"},
        draft_id=first.draft_id,
    )

    stale = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{first.draft_id}/accept",
        data={"version": 1},
    )
    assert "not the newest" in stale.text
    assert console.drafts()[0]["review_state"] == "draft"

    accepted = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{first.draft_id}/accept",
        data={"version": 2},
    )
    assert "Minutes recorded for this meeting." in accepted.text


def test_reviewing_an_unknown_draft_re_renders_with_the_service_message(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)

    page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/nope/accept",
        data={"version": 1},
    )

    assert page.status_code == 200
    assert "No draft nope for this meeting." in page.text


def test_the_transcript_pages(tmp_path: Path) -> None:
    console = _console(tmp_path)
    write_json(
        console.workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=tuple(
                Segment(
                    start=float(i), end=float(i) + 0.5, text=f"line {i}", source="a"
                )
                for i in range(5)
            ),
        ),
    )

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert "line 0" in page.text and "line 4" in page.text


def test_the_api_lists_and_accepts_a_draft(tmp_path: Path) -> None:
    console = _console(tmp_path)
    draft = console.write("transcript_check")

    listed = console.client.get(f"/api/meetings/{console.meeting.id}/agent")
    assert listed.status_code == 200
    assert listed.json()["kinds"] == [
        "glossary_collection",
        "transcript_check",
        "minutes",
    ]
    assert listed.json()["drafts"][0]["draft_id"] == draft.draft_id

    accepted = console.client.post(
        f"/api/meetings/{console.meeting.id}/agent/drafts/{draft.draft_id}/accept"
        f"?version={draft.version}"
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["review_state"] == "accepted"
    assert body["promotion"]["kind"] == "transcript_check"
    assert (
        console.registry.latest_artifact(console.meeting.id, "transcript_revision")
        is not None
    )


def test_an_unknown_draft_is_a_404(tmp_path: Path) -> None:
    console = _console(tmp_path)
    assert (
        console.client.post(
            f"/api/meetings/{console.meeting.id}/agent/drafts/nope/accept?version=1"
        ).status_code
        == 404
    )


def test_the_transcript_pane_names_the_run_that_produced_it(tmp_path: Path) -> None:
    """The pane is a reader too, so it says which run's transcript this is.

    The documents a run writes name it (ADR-0033); the pane showed the source,
    the segment count and the paging window but not the run, while the artifact
    table beside it and the API payload both carried it.
    """
    console = _console(tmp_path)
    scope = Workspace.at(console.workspace).begin_scope(7)
    scope.write_record(
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(Segment(start=3.0, end=4.0, text="run seven", source="a"),),
        )
    )
    publish_run(scope)

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert page.status_code == 200
    assert "run seven" in page.text
    assert "run 7" in page.text  # the pane's own line
