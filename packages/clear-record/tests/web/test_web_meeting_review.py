"""The console's meeting review surface: transcript, artifacts, agent drafts.

Ticket 16's view is server-rendered over the same service seam the API and MCP
use, so these tests drive it with a temp registry, a temp meeting workspace and
an **injected stub runner** — no endpoint, no model, no key. They assert the
page's contract: the transcript and artifacts are shown, each of the three tasks
can be launched, a draft is reviewable, and accepting it shows the promoted
result in place (and on the project page, for minutes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.service import (
    AgentConfig,
    Registry,
    Runner,
    RunnerOutput,
    RunnerRequest,
    RunManager,
)
from clear_record.web.app import create_app

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


class StubRunner(Runner):
    kind = "stub"

    def __init__(self, answers: dict[str, dict] | None = None) -> None:
        self.answers = answers or _ANSWERS

    def run(self, request: RunnerRequest) -> RunnerOutput:
        return RunnerOutput(
            text=json.dumps(self.answers[request.kind]), model="stub-model"
        )


#: Distinguishes "inject the stub runner" (the default) from "inject no runner",
#: which is the unconfigured-agent case.
_STUB = object()


def _console(tmp_path: Path, *, runner: Runner | None | object = _STUB, config=None):
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
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
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    app = create_app(
        registry,
        RunManager(registry),
        agent_runner=StubRunner() if runner is _STUB else runner,
        agent_config=config,
    )
    return SimpleConsole(
        client=TestClient(app), registry=registry, meeting=meeting, workspace=workspace
    )


class SimpleConsole:
    def __init__(self, *, client, registry, meeting, workspace) -> None:
        self.client = client
        self.registry = registry
        self.meeting = meeting
        self.workspace = workspace


def _launch(console: SimpleConsole, kind: str):
    return console.client.post(f"/ui/meetings/{console.meeting.id}/agent/{kind}")


def _drafts(console: SimpleConsole) -> list[dict]:
    res = console.client.get(f"/api/meetings/{console.meeting.id}/agent")
    assert res.status_code == 200
    return res.json()["drafts"]


def test_the_meeting_view_shows_the_transcript_and_artifacts(tmp_path: Path) -> None:
    console = _console(tmp_path)
    console.registry.add_artifact(
        console.meeting.id,
        kind="record",
        path=str(console.workspace / "record.json"),
        produced_by="pipeline",
        review_state="final",
    )

    page = console.client.get("/ui/projects/ops/meetings/kickoff")

    assert page.status_code == 200
    assert "the falcon is up" in page.text
    assert "record.json" in page.text
    assert "pipeline" in page.text  # the artifact's provenance
    assert "Collect glossary terms" in page.text


def test_the_project_view_links_to_the_meeting_review(tmp_path: Path) -> None:
    console = _console(tmp_path)
    page = console.client.get("/ui/projects/ops/meetings")
    assert "Review" in page.text
    assert 'href="/projects/ops/meetings/kickoff"' in page.text


@pytest.mark.parametrize("kind", ("glossary_collection", "transcript_check", "minutes"))
def test_each_task_launches_and_lands_as_a_reviewable_draft(
    kind: str, tmp_path: Path
) -> None:
    console = _console(tmp_path)

    page = _launch(console, kind)

    assert page.status_code == 200
    assert kind in page.text
    assert "Accept" in page.text and "Reject" in page.text
    drafts = _drafts(console)
    assert [(draft["kind"], draft["review_state"]) for draft in drafts] == [
        (kind, "draft")
    ]


def test_accepting_glossary_draft_adds_candidate_terms(tmp_path: Path) -> None:
    console = _console(tmp_path)
    _launch(console, "glossary_collection")
    run_id = _drafts(console)[0]["run_id"]

    page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{run_id}/accept"
    )

    assert page.status_code == 200
    assert "term(s) added" in page.text
    assert [
        (term.term, term.status) for term in console.registry.list_terms("ops")
    ] == [("Falcon", "candidate")]


def test_accepting_minutes_shows_them_per_meeting_and_across_the_project(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)
    _launch(console, "minutes")
    run_id = _drafts(console)[0]["run_id"]

    meeting_page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{run_id}/accept"
    )
    assert "We shipped it." in meeting_page.text
    assert "Minutes recorded for this meeting." in meeting_page.text

    project_page = console.client.get("/ui/projects/ops")
    assert "table-minutes" in project_page.text
    assert "Kickoff" in project_page.text


def test_rejecting_a_draft_keeps_it_and_promotes_nothing(tmp_path: Path) -> None:
    console = _console(tmp_path)
    _launch(console, "glossary_collection")
    run_id = _drafts(console)[0]["run_id"]

    page = console.client.post(
        f"/ui/meetings/{console.meeting.id}/agent/drafts/{run_id}/reject"
    )

    assert page.status_code == 200
    assert "rejected" in page.text
    assert console.registry.list_terms("ops") == []


def test_an_unconfigured_agent_says_so_and_refuses_cleanly(tmp_path: Path) -> None:
    console = _console(tmp_path, runner=None, config=AgentConfig())

    page = console.client.get("/ui/projects/ops/meetings/kickoff")
    assert "No agent is configured" in page.text

    refused = _launch(console, "minutes")
    assert refused.status_code == 200
    assert "no agent configured" in refused.text
    assert _drafts(console) == []


def test_launch_without_a_transcript_renders_the_service_message(
    tmp_path: Path,
) -> None:
    console = _console(tmp_path)
    (console.workspace / "record.json").unlink()

    page = _launch(console, "minutes")

    assert page.status_code == 200
    assert "no transcript" in page.text


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


def test_the_api_runs_and_accepts_a_draft(tmp_path: Path) -> None:
    console = _console(tmp_path)

    launched = console.client.post(
        f"/api/meetings/{console.meeting.id}/agent/transcript_check"
    )
    assert launched.status_code == 201
    run_id = launched.json()["run_id"]

    accepted = console.client.post(
        f"/api/meetings/{console.meeting.id}/agent/drafts/{run_id}/accept"
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["review_state"] == "accepted"
    assert body["promotion"]["kind"] == "transcript_check"
    assert (
        console.registry.latest_artifact(console.meeting.id, "transcript_revision")
        is not None
    )


def test_an_unknown_task_kind_is_a_404(tmp_path: Path) -> None:
    console = _console(tmp_path)
    assert (
        console.client.post(
            f"/api/meetings/{console.meeting.id}/agent/not-a-kind"
        ).status_code
        == 404
    )
