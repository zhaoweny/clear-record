"""What an accepted draft of each kind produces: the typed promotion.

The runner seam (``test_agent_runner.py``) and the task kinds
(``test_agent_task_drafts.py``) cover launching; this module covers the other
half of ADR-0018's review states — ``promote_draft`` and :class:`MeetingAgent`.
Each kind is driven through a stub runner (no endpoint, no model, no key), and
the assertions are about the **effect** of an acceptance, not the flag:

- ``glossary_collection`` → candidate registry terms, never confirmed ones;
- ``transcript_check`` → a new revision artifact, with the record left alone;
- ``minutes`` → the meeting's minutes artifact;
- and each promotion is idempotent, so re-accepting changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.service import (
    AGENT_DIRNAME,
    PROMOTERS,
    TASK_KINDS,
    TASKS,
    MeetingAgent,
    MeetingAgentError,
    Registry,
    Runner,
    RunnerOutput,
    RunnerRequest,
    describe_draft,
    project_snapshot,
    promote_draft,
)

_TRANSCRIPT = "00:00:03.000 [mic] the falcon is up"

_ANSWERS: dict[str, dict] = {
    "glossary_collection": {
        "terms": [
            {
                "term": "Falcon",
                "reading": "FAL-kun",
                "aliases": ["falcon"],
                "definition": "a tracked object",
                "evidence": _TRANSCRIPT,
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
    """A runner that returns one canned contract-valid answer per kind."""

    kind = "stub"

    def __init__(self, answers: dict[str, dict] | None = None) -> None:
        self.answers = answers or _ANSWERS

    def run(self, request: RunnerRequest) -> RunnerOutput:
        return RunnerOutput(
            text=json.dumps(self.answers[request.kind]), model="stub-model"
        )


def _workspace(tmp_path: Path) -> tuple[Registry, object, Path]:
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
    return registry, meeting, workspace


def _agent(registry: Registry, meeting, runner: Runner | None = None) -> MeetingAgent:
    return MeetingAgent(registry, meeting, runner=runner or StubRunner())


# --- the dispatch tables cover every declared kind -------------------------- #


def test_every_declared_kind_has_a_builder_and_a_promoter() -> None:
    """A kind is complete only with both halves: a task builder and a promoter.

    The dispatch tables are lookups with named errors, never fallthroughs, so a
    fourth kind added to :data:`TASK_KINDS` fails loudly at the table and here
    rather than being silently packaged or promoted as another kind.
    """
    assert set(TASK_KINDS) == set(TASKS) == set(PROMOTERS)


# --- launching -------------------------------------------------------------- #


def test_launch_packages_the_meeting_context(tmp_path: Path) -> None:
    registry, meeting, _workspace_path = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    draft = agent.launch("transcript_check")

    assert draft.review_state == "draft"
    assert draft.project == "ops" and draft.meeting == "kickoff"
    # The transcript reached the task; the project glossary section is present.
    assert draft.provenance.kind == "transcript_check"
    packaged = json.loads((draft.run_dir / "input.json").read_text(encoding="utf-8"))
    assert "the falcon is up" in packaged["inputs"]["transcript"]
    assert "glossary" in packaged["inputs"] and "context" in packaged["inputs"]
    # A draft directory belongs to the meeting's agent directory.
    assert draft.run_dir.parent == _workspace_path / AGENT_DIRNAME


def test_launch_without_a_transcript_says_so(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    (Path(meeting.workspace_path) / "record.json").unlink()

    with pytest.raises(MeetingAgentError, match="no transcript"):
        _agent(registry, meeting).launch("minutes")


def test_drafts_lists_only_this_meetings_runs(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    agent.launch("minutes")
    (agent.directory / "unrelated" / "run.json").parent.mkdir(parents=True)
    (agent.directory / "unrelated" / "run.json").write_text("{}", encoding="utf-8")

    drafts = agent.drafts()

    assert [draft.kind for draft in drafts] == ["minutes"]


# --- promotion: glossary collection ----------------------------------------- #


def test_promoting_glossary_candidates_adds_candidate_terms(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = agent.launch("glossary_collection")
    assert registry.list_terms("ops") == []  # a launch alone changes nothing

    promoted = agent.promote(draft)

    terms = registry.list_terms("ops")
    assert [(term.term, term.status, term.added_by) for term in terms] == [
        ("Falcon", "candidate", "agent")
    ]
    assert terms[0].reading == "FAL-kun"
    assert terms[0].aliases == "falcon"
    assert promoted.review_state == "accepted"
    assert promoted.promotion["summary"]["added"] == ["Falcon"]
    # Candidate terms are excluded from the decoder snapshot: an accepted draft
    # is not the owner's per-term confirmation.
    assert project_snapshot(registry, "ops").empty


def test_promoting_glossary_twice_is_idempotent(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = agent.launch("glossary_collection")
    once = agent.promote(draft)

    twice = agent.promote(once)

    assert len(registry.list_terms("ops")) == 1
    assert twice.promotion == once.promotion


def test_promoting_glossary_skips_an_existing_term(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    registry.add_term("ops", "Falcon", status="confirmed", added_by="human")
    agent = _agent(registry, meeting)

    promoted = agent.promote(agent.launch("glossary_collection"))

    assert promoted.promotion["summary"] == {
        "added": [],
        "skipped": ["Falcon"],
        "status": "candidate",
    }
    # The owner's confirmed term is untouched.
    assert registry.list_terms("ops")[0].status == "confirmed"


# --- promotion: transcript check -------------------------------------------- #


def test_promoting_a_check_records_a_revision_without_overwriting_the_record(
    tmp_path: Path,
) -> None:
    registry, meeting, workspace = _workspace(tmp_path)
    record_before = (workspace / "record.json").read_bytes()
    agent = _agent(registry, meeting)

    promoted = agent.promote(agent.launch("transcript_check"))

    summary = promoted.promotion["summary"]
    revision = Path(summary["revision_path"])
    assert (
        revision.read_text(encoding="utf-8").strip()
        == _ANSWERS["transcript_check"]["revision"]
    )
    assert summary["changes"] == 1
    changes = json.loads(Path(summary["changes_path"]).read_text(encoding="utf-8"))
    assert changes["changes"] == _ANSWERS["transcript_check"]["changes"]
    assert changes["provenance"]["runner"] == "stub"
    # The reconciled record is not the promotion's target: a revision is a new file.
    assert (workspace / "record.json").read_bytes() == record_before
    artifact = registry.latest_artifact(meeting.id, "transcript_revision")
    assert artifact is not None and artifact.produced_by == "agent"
    assert artifact.path == str(revision)


def test_promoting_a_check_twice_registers_one_artifact(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    once = agent.promote(agent.launch("transcript_check"))

    agent.promote(once)

    assert len(registry.list_artifacts(meeting.id)) == 1


# --- promotion: minutes ----------------------------------------------------- #


def test_promoting_minutes_records_the_meetings_minutes(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    promoted = agent.promote(agent.launch("minutes"))

    artifact = registry.latest_artifact(meeting.id, "minutes")
    assert artifact is not None and artifact.kind == "minutes"
    assert artifact.produced_by == "agent"
    assert Path(artifact.path).read_text(encoding="utf-8").strip() == (
        "# Kickoff\n\nWe shipped it."
    )
    assert promoted.promotion["summary"]["decisions"] == 1
    assert promoted.promotion["summary"]["actions"] == 1


def test_a_second_accepted_minutes_draft_supersedes_the_first(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    agent.promote(agent.launch("minutes"))

    other = StubRunner(
        {
            "minutes": {
                **_ANSWERS["minutes"],
                "body": "# Kickoff (revised)",
            }
        }
    )
    promoted = MeetingAgent(registry, meeting, runner=other).promote(
        MeetingAgent(registry, meeting, runner=other).launch("minutes")
    )

    latest = registry.latest_artifact(meeting.id, "minutes")
    assert (
        Path(latest.path).read_text(encoding="utf-8").strip() == "# Kickoff (revised)"
    )
    assert promoted.promotion["summary"]["artifact_id"] == latest.id


# --- review states ---------------------------------------------------------- #


def test_rejecting_a_draft_keeps_it_and_promotes_nothing(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = agent.launch("glossary_collection")

    rejected = agent.reject(draft)

    assert rejected.review_state == "rejected"
    assert registry.list_terms("ops") == []
    assert registry.list_artifacts(meeting.id) == []
    assert agent.draft(draft.provenance.run_id).review_state == "rejected"


def test_read_draft_round_trips_the_promotion_record(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    promoted = agent.promote(agent.launch("minutes"))

    reloaded = agent.draft(promoted.provenance.run_id)

    assert reloaded.review_state == "accepted"
    assert reloaded.promotion == promoted.promotion
    assert describe_draft(reloaded).promotion["kind"] == "minutes"


def test_promote_draft_needs_a_workspace(tmp_path: Path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    draft = MeetingAgent(registry, meeting, runner=StubRunner())
    with pytest.raises(MeetingAgentError, match="no workspace"):
        draft.launch("minutes")


def test_promote_draft_is_the_same_function_the_agent_delegates_to(
    tmp_path: Path,
) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = agent.launch("minutes")

    promoted = promote_draft(draft, registry=registry, meeting=meeting)

    assert promoted.review_state == "accepted"
    assert registry.latest_artifact(meeting.id, "minutes") is not None
