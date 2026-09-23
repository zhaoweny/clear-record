"""The draft store and what an acceptance of each kind produces.

The store is a **version chain with author provenance** (ADR-0031): a harness
writes what it produced, the app stores it as it came, and a human's acceptance
is recorded on the version it decided. This module covers both halves:

- the chain: who wrote a version, what a new version does to a decided draft,
  and how an acceptance is recorded;
- the promotion: what accepting a version of each kind actually produces, and
  that it is idempotent.

Nothing here needs a model, an endpoint or a key: the drive
(``scripts/agent_drive.py``) exercises the MCP surface, and this covers the
service semantics underneath it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clear_record.service import (
    AGENT_DIRNAME,
    PROMOTERS,
    TASK_KINDS,
    Draft,
    MeetingAgent,
    MeetingAgentError,
    PromotionError,
    Registry,
    describe_draft,
    project_snapshot,
    promote_draft,
    start_draft,
)

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

_AUTHOR = "pi-agent"


def _workspace(tmp_path: Path) -> tuple[Registry, object, Path]:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    return registry, meeting, workspace


def _agent(registry: Registry, meeting) -> MeetingAgent:
    return MeetingAgent(registry, meeting)


def _write(agent: MeetingAgent, kind: str, value: dict | None = None) -> Draft:
    return agent.write(
        kind, value if value is not None else _ANSWERS[kind], author=_AUTHOR
    )


# --- the dispatch tables cover every declared kind -------------------------- #


def test_every_declared_kind_has_a_promoter() -> None:
    """A kind is complete only with a promoter: the table is a lookup with a
    named error and a test, never a fallthrough, so a fourth kind added to
    :data:`TASK_KINDS` fails here rather than being silently promoted as another.
    """
    assert set(TASK_KINDS) == set(PROMOTERS)


# --- the chain: what a version records -------------------------------------- #


def test_a_written_draft_records_its_author_and_value(tmp_path: Path) -> None:
    registry, meeting, workspace = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    draft = _write(agent, "transcript_check")

    assert draft.review_state == "draft"
    assert draft.project == "ops" and draft.meeting == "kickoff"
    assert draft.value == _ANSWERS["transcript_check"]
    assert draft.provenance.author == _AUTHOR
    assert draft.provenance.written_at
    assert len(draft.versions) == 1
    # A draft directory belongs to the meeting's agent directory.
    assert draft.run_dir.parent == workspace / AGENT_DIRNAME
    # Writing needs no transcript: the harness read it over MCP.
    assert not (workspace / "record.json").exists()


def test_a_new_version_reopens_a_decided_draft(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    accepted = agent.promote(first)
    assert accepted.review_state == "accepted"

    revised = _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"}
    second = agent.write(
        "minutes", revised, author="another-agent", draft_id=first.draft_id
    )

    assert [version.provenance.author for version in second.versions] == [
        _AUTHOR,
        "another-agent",
    ]
    # The decision belongs to the version it was made on, so the chain is back
    # in review — a new version is never accepted on an earlier one's strength.
    assert second.review_state == "draft"
    assert second.value["body"] == "# Kickoff (revised)"
    assert agent.draft(first.draft_id).versions[0].reviewed_by == "human"


def test_appending_to_an_unknown_chain_is_a_named_error(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)

    with pytest.raises(MeetingAgentError, match="no draft"):
        _agent(registry, meeting).write(
            "minutes", _ANSWERS["minutes"], author=_AUTHOR, draft_id="nope"
        )


def test_an_unknown_kind_is_a_named_error(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)

    with pytest.raises(MeetingAgentError, match="unknown draft kind"):
        _agent(registry, meeting).write("summarize", {}, author=_AUTHOR)


def test_drafts_lists_only_this_meetings_chains(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    other = registry.create_meeting(
        "ops", "Standup", workspace_path=str(tmp_path / "ws2")
    )
    agent = _agent(registry, meeting)
    _write(agent, "minutes")
    _agent(registry, other).write("minutes", _ANSWERS["minutes"], author=_AUTHOR)
    # A directory that is not a chain at all is skipped, not fatal.
    (agent.directory / "unrelated").mkdir(parents=True)

    drafts = agent.drafts()

    assert [draft.kind for draft in drafts] == ["minutes"]
    assert len(list(agent.directory.iterdir())) == 2


def test_writing_needs_a_workspace(tmp_path: Path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")

    with pytest.raises(MeetingAgentError, match="no workspace"):
        _agent(registry, meeting).write("minutes", _ANSWERS["minutes"], author=_AUTHOR)


# --- promotion: glossary collection ----------------------------------------- #


def test_promoting_glossary_candidates_adds_candidate_terms(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = _write(agent, "glossary_collection")
    assert registry.list_terms("ops") == []  # a write alone changes nothing

    promoted = agent.promote(draft)

    terms = registry.list_terms("ops")
    assert [(term.term, term.status, term.added_by) for term in terms] == [
        ("Falcon", "candidate", "agent")
    ]
    assert terms[0].reading == "FAL-kun"
    assert terms[0].aliases == "falcon"
    assert promoted.review_state == "accepted"
    assert promoted.promotion["summary"]["added"] == ["Falcon"]
    # The chain records who decided it, beside who wrote it.
    assert promoted.versions[-1].reviewed_by == "human"
    assert promoted.versions[-1].provenance.author == _AUTHOR
    # Candidate terms are excluded from the decoder snapshot: an accepted draft
    # is not the owner's per-term confirmation.
    assert project_snapshot(registry, "ops").empty


def test_promoting_glossary_twice_is_idempotent(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    once = agent.promote(_write(agent, "glossary_collection"))

    twice = agent.promote(once)

    assert len(registry.list_terms("ops")) == 1
    assert twice.promotion == once.promotion


def test_promoting_glossary_skips_an_existing_term(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    registry.add_term("ops", "Falcon", status="confirmed", added_by="human")
    agent = _agent(registry, meeting)

    promoted = agent.promote(_write(agent, "glossary_collection"))

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
    (workspace / "record.json").write_text('{"segments": []}\n', encoding="utf-8")
    record_before = (workspace / "record.json").read_bytes()
    agent = _agent(registry, meeting)

    promoted = agent.promote(_write(agent, "transcript_check"))

    summary = promoted.promotion["summary"]
    revision = Path(summary["revision_path"])
    assert (
        revision.read_text(encoding="utf-8").strip()
        == _ANSWERS["transcript_check"]["revision"]
    )
    assert summary["changes"] == 1
    changes = json.loads(Path(summary["changes_path"]).read_text(encoding="utf-8"))
    assert changes["changes"] == _ANSWERS["transcript_check"]["changes"]
    assert changes["author"] == _AUTHOR
    # The reconciled record is not the promotion's target: a revision is a new file.
    assert (workspace / "record.json").read_bytes() == record_before
    artifact = registry.latest_artifact(meeting.id, "transcript_revision")
    assert artifact is not None and artifact.produced_by == "agent"
    assert artifact.path == str(revision)


def test_promoting_a_check_twice_registers_one_artifact(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    once = agent.promote(_write(agent, "transcript_check"))

    agent.promote(once)

    assert len(registry.list_artifacts(meeting.id)) == 1


# --- promotion: minutes ----------------------------------------------------- #


def test_promoting_minutes_records_the_meetings_minutes(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    promoted = agent.promote(_write(agent, "minutes"))

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
    agent.promote(_write(agent, "minutes"))

    revised = {"minutes": _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"}}
    promoted = agent.promote(_write(agent, "minutes", revised["minutes"]))

    latest = registry.latest_artifact(meeting.id, "minutes")
    assert (
        Path(latest.path).read_text(encoding="utf-8").strip() == "# Kickoff (revised)"
    )
    assert promoted.promotion["summary"]["artifact_id"] == latest.id


# --- review ----------------------------------------------------------------- #


def test_rejecting_a_draft_keeps_it_and_promotes_nothing(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = _write(agent, "glossary_collection")

    rejected = agent.reject(draft)

    assert rejected.review_state == "rejected"
    assert rejected.promotion is None
    assert registry.list_terms("ops") == []
    assert registry.list_artifacts(meeting.id) == []
    stored = agent.draft(draft.draft_id)
    assert stored.review_state == "rejected"
    assert stored.versions[-1].reviewed_by == "human"


def test_a_review_round_trips_the_promotion_record(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    promoted = agent.promote(_write(agent, "minutes"))

    reloaded = agent.draft(promoted.draft_id)

    assert reloaded.review_state == "accepted"
    assert reloaded.promotion == promoted.promotion
    assert describe_draft(reloaded).promotion["kind"] == "minutes"
    assert describe_draft(reloaded).versions[0].author == _AUTHOR


def test_a_value_that_cannot_be_its_kind_is_refused_at_the_write(
    tmp_path: Path,
) -> None:
    """A payload the acceptance cannot read is refused where it is written.

    Without this, ``write_agent_draft(kind='minutes', value={})`` accepts and
    registers a 1-byte minutes document as the meeting's final minutes.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    with pytest.raises(MeetingAgentError, match="'body' is missing"):
        agent.write("minutes", {}, author=_AUTHOR)
    with pytest.raises(MeetingAgentError, match="'terms' must be a list"):
        agent.write("glossary_collection", {"terms": "Falcon"}, author=_AUTHOR)
    with pytest.raises(MeetingAgentError, match=r"terms\[0\]\.term"):
        agent.write("glossary_collection", {"terms": [{}]}, author=_AUTHOR)
    with pytest.raises(MeetingAgentError, match="'changes'"):
        agent.write("transcript_check", {"revision": "x"}, author=_AUTHOR)
    # A well-shaped empty list is legitimate: the harness found nothing.
    assert (
        agent.write("glossary_collection", {"terms": []}, author=_AUTHOR).review_state
        == "draft"
    )
    assert agent.drafts()  # and nothing above left a partial write behind


def test_a_chain_that_does_not_hold_its_kind_cannot_be_accepted(
    tmp_path: Path,
) -> None:
    """Accept stays honest for a chain stored without the write-path check.

    A 0.3 draft is checked when it is written, but a hand-written or older chain
    can still hold anything; accepting one must not register an artifact that is
    not the thing its kind promises.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = start_draft(
        agent.directory,
        kind="minutes",
        project="ops",
        meeting="kickoff",
        value={},
        author=_AUTHOR,
    )

    with pytest.raises(PromotionError, match="'body' is missing"):
        agent.promote(draft)

    assert registry.list_artifacts(meeting.id) == []
    assert not (draft.run_dir / "minutes.md").exists()


def test_a_decision_names_the_version_the_human_read(tmp_path: Path) -> None:
    """A version a harness appended in the meantime is a refusal, not a decision."""
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"},
        author="another-agent",
        draft_id=first.draft_id,
    )
    read = agent.draft(first.draft_id)
    assert read.version == 2

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.promote(read, version=1)

    # Nothing ran: the stale refusal is checked before any side effect.
    assert registry.list_artifacts(meeting.id) == []

    promoted = agent.promote(agent.draft(first.draft_id), version=2)

    assert promoted.review_state == "accepted"
    artifact = registry.latest_artifact(meeting.id, "minutes")
    assert artifact is not None
    assert "(revised)" in Path(artifact.path).read_text(encoding="utf-8")


def test_a_rejection_names_its_version_too(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    agent.write(
        "minutes", _ANSWERS["minutes"], author="another-agent", draft_id=first.draft_id
    )

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.reject(agent.draft(first.draft_id), version=1)

    assert agent.draft(first.draft_id).review_state == "draft"


def test_two_writes_in_one_second_open_two_chains(tmp_path: Path) -> None:
    """A second write is a new chain, never an append to a chain by accident.

    The id is a digest of the facts, so two writes inside one clock second used
    to land on one directory — silently re-opening a decided draft.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    frozen = "2026-01-01T00:00:00+00:00"
    clock = lambda: frozen  # noqa: E731 - one frozen instant for both writes

    first = agent.write("minutes", _ANSWERS["minutes"], author=_AUTHOR, clock=clock)
    second = agent.write("minutes", _ANSWERS["minutes"], author=_AUTHOR, clock=clock)

    assert first.draft_id != second.draft_id
    assert len(agent.drafts()) == 2
    assert all(len(draft.versions) == 1 for draft in agent.drafts())


def test_legacy_0_2_runs_are_named_not_migrated(tmp_path: Path) -> None:
    """A 0.2 run directory is reported as such, and is not part of the chain."""
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    legacy = agent.directory / "minutes-abc123"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text("{}", encoding="utf-8")
    _write(agent, "minutes")

    assert agent.legacy_drafts() == ("minutes-abc123",)
    assert [draft.kind for draft in agent.drafts()] == ["minutes"]


def test_legacy_0_2_runs_are_empty_when_there_are_none(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    assert _agent(registry, meeting).legacy_drafts() == ()


def test_promote_draft_is_the_same_function_the_agent_delegates_to(
    tmp_path: Path,
) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = _write(agent, "minutes")

    promoted = promote_draft(draft, registry=registry, meeting=meeting)

    assert promoted.review_state == "accepted"
    assert registry.latest_artifact(meeting.id, "minutes") is not None
