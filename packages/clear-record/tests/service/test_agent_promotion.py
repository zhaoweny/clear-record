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

import hashlib
import json
from pathlib import Path

import pytest

from clear_record.service import (
    AGENT_DIRNAME,
    CONSOLE,
    DRAFT_FILENAME,
    MCP,
    PROMOTERS,
    TASK_KINDS,
    Draft,
    MeetingAgent,
    MeetingAgentError,
    PromotionError,
    Registry,
    UnreadableChain,
    describe_draft,
    project_snapshot,
    promote_draft,
    start_draft,
)
from clear_record.service import agent_drafts as store

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

#: The two transports these tests call as. A draft's recorded author and its
#: reviewer are the **actors** their transports supply, never declared identities
#: (ADR-0033): the harness writes over ``mcp``, a person decides at the console.
_HARNESS = MCP
_HUMAN = CONSOLE


def _workspace(tmp_path: Path) -> tuple[Registry, object, Path]:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor=_HUMAN,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor=_HUMAN,
    )
    return registry, meeting, workspace


def _agent(registry: Registry, meeting) -> MeetingAgent:
    return MeetingAgent(registry, meeting)


def _write(agent: MeetingAgent, kind: str, value: dict | None = None) -> Draft:
    return agent.write(
        kind,
        value if value is not None else _ANSWERS[kind],
        actor=_HARNESS,
    )


def _promote(agent: MeetingAgent, draft: Draft) -> Draft:
    """Accept the version this draft was read at — what a reviewer who read it does."""
    return agent.promote(
        draft,
        version=draft.version,
        actor=_HUMAN,
    )


def _reject(agent: MeetingAgent, draft: Draft) -> Draft:
    """Reject the version this draft was read at."""
    return agent.reject(
        draft,
        version=draft.version,
        actor=_HUMAN,
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
    assert draft.provenance.author == _HARNESS
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
    accepted = _promote(agent, first)
    assert accepted.review_state == "accepted"

    revised = _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"}
    second = agent.write(
        "minutes",
        revised,
        draft_id=first.draft_id,
        actor=_HUMAN,
    )

    assert [version.provenance.author for version in second.versions] == [
        _HARNESS,
        _HUMAN,
    ]
    # The decision belongs to the version it was made on, so the chain is back
    # in review — a new version is never accepted on an earlier one's strength.
    assert second.review_state == "draft"
    assert second.value["body"] == "# Kickoff (revised)"
    assert agent.draft(first.draft_id).versions[0].reviewed_by == _HUMAN


def test_appending_to_an_unknown_chain_is_a_named_error(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)

    with pytest.raises(MeetingAgentError, match="no draft"):
        _agent(registry, meeting).write(
            "minutes", _ANSWERS["minutes"], draft_id="nope", actor=_HARNESS
        )


def test_an_unknown_kind_is_a_named_error(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)

    with pytest.raises(MeetingAgentError, match="unknown draft kind"):
        _agent(registry, meeting).write("summarize", {}, actor=_HARNESS)


def test_drafts_lists_only_this_meetings_chains(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    other = registry.create_meeting(
        "ops",
        "Standup",
        workspace_path=str(tmp_path / "ws2"),
        actor=_HUMAN,
    )
    agent = _agent(registry, meeting)
    _write(agent, "minutes")
    _agent(registry, other).write("minutes", _ANSWERS["minutes"], actor=_HARNESS)
    # A directory that is not a chain at all is skipped, not fatal.
    (agent.directory / "unrelated").mkdir(parents=True)

    drafts = agent.drafts()

    assert [draft.kind for draft in drafts] == ["minutes"]
    assert len(list(agent.directory.iterdir())) == 2


def test_writing_needs_a_workspace(tmp_path: Path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor=_HUMAN,
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        actor=_HUMAN,
    )

    with pytest.raises(MeetingAgentError, match="no workspace"):
        _agent(registry, meeting).write("minutes", _ANSWERS["minutes"], actor=_HARNESS)


# --- promotion: glossary collection ----------------------------------------- #


def test_promoting_glossary_candidates_adds_candidate_terms(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = _write(agent, "glossary_collection")
    assert registry.list_terms("ops") == []  # a write alone changes nothing

    promoted = _promote(agent, draft)

    terms = registry.list_terms("ops")
    assert [(term.term, term.status, term.added_by) for term in terms] == [
        ("Falcon", "candidate", "agent")
    ]
    assert terms[0].reading == "FAL-kun"
    assert terms[0].aliases == "falcon"
    assert promoted.review_state == "accepted"
    assert promoted.promotion["summary"]["added"] == ["Falcon"]
    # The chain records who decided it, beside who wrote it.
    assert promoted.versions[-1].reviewed_by == _HUMAN
    assert promoted.versions[-1].provenance.author == _HARNESS
    # Candidate terms are excluded from the decoder snapshot: an accepted draft
    # is not the owner's per-term confirmation.
    assert project_snapshot(registry, "ops").empty


def test_promoting_glossary_twice_is_idempotent(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    once = _promote(agent, _write(agent, "glossary_collection"))

    twice = _promote(agent, once)

    assert len(registry.list_terms("ops")) == 1
    assert twice.promotion == once.promotion


def test_promoting_glossary_skips_an_existing_term(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    registry.add_term(
        "ops",
        "Falcon",
        status="confirmed",
        added_by="human",
        actor=_HUMAN,
    )
    agent = _agent(registry, meeting)

    promoted = _promote(agent, _write(agent, "glossary_collection"))

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

    promoted = _promote(agent, _write(agent, "transcript_check"))

    summary = promoted.promotion["summary"]
    revision = Path(summary["revision_path"])
    assert (
        revision.read_text(encoding="utf-8").strip()
        == _ANSWERS["transcript_check"]["revision"]
    )
    assert summary["changes"] == 1
    changes = json.loads(Path(summary["changes_path"]).read_text(encoding="utf-8"))
    assert changes["changes"] == _ANSWERS["transcript_check"]["changes"]
    assert changes["author"] == _HARNESS
    # The reconciled record is not the promotion's target: a revision is a new file.
    assert (workspace / "record.json").read_bytes() == record_before
    artifact = registry.latest_artifact(meeting.id, "transcript_revision")
    assert artifact is not None and artifact.produced_by == "agent"
    assert artifact.path == str(revision)


def test_promoting_a_check_twice_registers_one_artifact(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    once = _promote(agent, _write(agent, "transcript_check"))

    _promote(agent, once)

    assert len(registry.list_artifacts(meeting.id)) == 1


# --- promotion: minutes ----------------------------------------------------- #


def test_promoting_minutes_records_the_meetings_minutes(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    promoted = _promote(agent, _write(agent, "minutes"))

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
    _promote(agent, _write(agent, "minutes"))

    revised = {"minutes": _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"}}
    promoted = _promote(agent, _write(agent, "minutes", revised["minutes"]))

    latest = registry.latest_artifact(meeting.id, "minutes")
    assert (
        Path(latest.path).read_text(encoding="utf-8").strip() == "# Kickoff (revised)"
    )
    assert promoted.promotion["summary"]["artifact_id"] == latest.id


def test_a_re_accepted_version_leaves_a_row_that_describes_the_file(
    tmp_path: Path,
) -> None:
    """A second version of one chain is promoted to the same path as the first.

    Reusing the row by ``(kind, path)`` would leave the first version's
    ``sha256``/``bytes`` on the artifact the console, the API and the archive read,
    while the file holds the second version's text.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _promote(agent, _write(agent, "minutes"))
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )

    assert agent.promote(
        agent.draft(first.draft_id),
        version=2,
        actor=_HUMAN,
    ).accepted

    latest = registry.latest_artifact(meeting.id, "minutes")
    assert latest is not None
    body = Path(latest.path).read_bytes()
    assert body.decode("utf-8").strip() == "# Kickoff (revised)"
    assert latest.sha256 == hashlib.sha256(body).hexdigest()
    assert latest.bytes == len(body)


def test_a_version_that_returns_to_earlier_bytes_gets_its_own_row(
    tmp_path: Path,
) -> None:
    """The newest row for a path always describes the file at that path.

    A tuning loop returns to an earlier version's bytes by design. Reusing a row by
    content alone would republish an *earlier* version's digest while the file
    holds the newest — the digest the console, the API and MCP all read.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first_body = _ANSWERS["minutes"]["body"]
    first = _write(agent, "minutes")
    _promote(agent, first)

    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": first_body + "\n\nAnd then some."},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )
    assert _promote(agent, agent.draft(first.draft_id)).accepted
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": first_body},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )
    assert _promote(agent, agent.draft(first.draft_id)).accepted

    latest = registry.latest_artifact(meeting.id, "minutes")
    assert latest is not None
    body = Path(latest.path).read_bytes()
    assert body.decode("utf-8").strip() == first_body
    assert latest.sha256 == hashlib.sha256(body).hexdigest()
    assert latest.bytes == len(body)


# --- review ----------------------------------------------------------------- #


def test_rejecting_a_draft_keeps_it_and_promotes_nothing(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    draft = _write(agent, "glossary_collection")

    rejected = _reject(agent, draft)

    assert rejected.review_state == "rejected"
    assert rejected.promotion is None
    assert registry.list_terms("ops") == []
    assert registry.list_artifacts(meeting.id) == []
    stored = agent.draft(draft.draft_id)
    assert stored.review_state == "rejected"
    assert stored.versions[-1].reviewed_by == _HUMAN


def test_a_review_round_trips_the_promotion_record(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    promoted = _promote(agent, _write(agent, "minutes"))

    reloaded = agent.draft(promoted.draft_id)

    assert reloaded.review_state == "accepted"
    assert reloaded.promotion == promoted.promotion
    assert describe_draft(reloaded).promotion["kind"] == "minutes"
    assert describe_draft(reloaded).versions[0].author == _HARNESS


def test_a_value_that_cannot_be_its_kind_is_refused_at_the_write(
    tmp_path: Path,
) -> None:
    """A payload the acceptance cannot read is refused where it is written.

    Without this, ``write_agent_draft(kind='minutes', value={})`` accepts and
    registers an artifact no larger than its own newline as the meeting's final
    minutes.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)

    with pytest.raises(MeetingAgentError, match="'body' is missing"):
        agent.write(
            "minutes",
            {},
            actor=_HARNESS,
        )
    # A blank string is the empty document the key rules out, not a value: without
    # this an empty (or whitespace) minutes body accepts and registers an artifact
    # no larger than its own newline as the meeting's final minutes.
    with pytest.raises(MeetingAgentError, match="'body' is empty"):
        agent.write(
            "minutes",
            {"body": "  "},
            actor=_HARNESS,
        )
    with pytest.raises(MeetingAgentError, match="'revision' is empty"):
        agent.write(
            "transcript_check",
            {"revision": "", "changes": []},
            actor=_HARNESS,
        )
    with pytest.raises(MeetingAgentError, match="'terms' must be a list"):
        agent.write(
            "glossary_collection",
            {"terms": "Falcon"},
            actor=_HARNESS,
        )
    with pytest.raises(MeetingAgentError, match=r"terms\[0\]\.term"):
        agent.write(
            "glossary_collection",
            {"terms": [{}]},
            actor=_HARNESS,
        )
    with pytest.raises(MeetingAgentError, match="'changes'"):
        agent.write(
            "transcript_check",
            {"revision": "x"},
            actor=_HARNESS,
        )
    # A well-shaped empty list is legitimate: the harness found nothing.
    assert (
        agent.write(
            "glossary_collection",
            {"terms": []},
            actor=_HARNESS,
        ).review_state
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
        actor=_HARNESS,
    )

    with pytest.raises(PromotionError, match="'body' is missing"):
        _promote(agent, draft)

    assert registry.list_artifacts(meeting.id) == []
    assert not (draft.run_dir / "minutes.md").exists()

    # An empty document is not a minutes document either: the check before the
    # promotion reads the same shape rule the write does.
    blank = start_draft(
        agent.directory,
        kind="minutes",
        project="ops",
        meeting="kickoff",
        value={"body": "  "},
        actor=_HARNESS,
    )

    with pytest.raises(PromotionError, match="'body' is empty"):
        _promote(agent, blank)

    assert registry.list_artifacts(meeting.id) == []


def test_a_decision_names_the_version_the_human_read(tmp_path: Path) -> None:
    """A version a harness appended in the meantime is a refusal, not a decision."""
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )
    read = agent.draft(first.draft_id)
    assert read.version == 2

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.promote(
            read,
            version=1,
            actor=_HUMAN,
        )

    # Nothing ran: the stale refusal is checked before any side effect.
    assert registry.list_artifacts(meeting.id) == []

    promoted = agent.promote(
        agent.draft(first.draft_id),
        version=2,
        actor=_HUMAN,
    )

    assert promoted.review_state == "accepted"
    artifact = registry.latest_artifact(meeting.id, "minutes")
    assert artifact is not None
    assert "(revised)" in Path(artifact.path).read_text(encoding="utf-8")


def test_a_rejection_names_its_version_too(tmp_path: Path) -> None:
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    agent.write(
        "minutes",
        _ANSWERS["minutes"],
        draft_id=first.draft_id,
        actor=_HARNESS,
    )

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.reject(
            agent.draft(first.draft_id),
            version=1,
            actor=_HUMAN,
        )

    assert agent.draft(first.draft_id).review_state == "draft"


def test_a_decision_refuses_a_chain_that_gained_a_version_after_the_read(
    tmp_path: Path,
) -> None:
    """A decision is written as the whole chain, so it must not drop a version.

    The snapshot was read before the harness appended, and the version it names is
    checked against the chain **on disk** — the case a check against the caller's
    own snapshot cannot see, and the one where a rewrite would erase the harness's
    text.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    read = agent.draft(first.draft_id)
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.reject(
            read,
            version=1,
            actor=_HUMAN,
        )

    stored = agent.draft(first.draft_id)
    assert stored.version == 2
    assert stored.review_state == "draft"
    assert stored.versions[1].value["body"] == "# Kickoff (revised)"


def test_a_superseded_acceptance_promotes_nothing(tmp_path: Path) -> None:
    """A refused acceptance leaves the meeting exactly as it was.

    The decision is refused **and** nothing is promoted: no minutes artifact, no
    ``minutes.md``. A refusal taken after the promoter ran would leave a final
    minutes artifact for a decision the console told the user was not recorded.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    read = agent.draft(first.draft_id)
    agent.write(
        "minutes",
        _ANSWERS["minutes"] | {"body": "# Kickoff (revised)"},
        draft_id=first.draft_id,
        actor=_HARNESS,
    )

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.promote(
            read,
            version=1,
            actor=_HUMAN,
        )

    assert registry.list_artifacts(meeting.id) == []
    assert not (first.run_dir / "minutes.md").exists()
    stored = agent.draft(first.draft_id)
    assert [version.decision for version in stored.versions] == [None, None]


def test_a_decision_already_recorded_is_returned_not_refused(tmp_path: Path) -> None:
    """Deciding a version that already carries a decision is a no-op, not a ``StaleVersion``.

    The second call's snapshot was read before the first decision landed, so it
    still reads ``draft`` where the chain reads ``accepted``. That is the decision
    recorded once (ADR-0031): the chain is returned as it stands, and a stale
    snapshot's own answer does not overwrite it.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    read = agent.draft(first.draft_id)
    accepted = _promote(agent, read)

    again = _promote(agent, read)
    still = _reject(agent, read)

    assert again.review_state == "accepted"
    assert again.promotion == accepted.promotion
    assert still.review_state == "accepted"
    assert len(registry.list_artifacts(meeting.id)) == 1


def test_a_chain_that_cannot_be_read_is_refused_not_written_over(
    tmp_path: Path,
) -> None:
    """A half-written ``draft.json`` is a refusal: the decision does not replace it.

    Another writer's torn file used to read as "nothing to lose", so a decision
    wrote over it. The refusal is the service's own sentence — the store raises
    :class:`UnreadableChain` and the agent maps it — so every surface that can
    reach it (a file damaged between its read and the store's hold) answers with
    words rather than an unhandled error.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    read = agent.draft(_write(agent, "minutes").draft_id)
    torn = read.path.read_text(encoding="utf-8")[:120]
    read.path.write_text(torn, encoding="utf-8")

    with pytest.raises(MeetingAgentError, match="cannot be read"):
        agent.reject(
            read,
            version=1,
            actor=_HUMAN,
        )

    assert read.path.read_text(encoding="utf-8") == torn


def test_appending_to_a_chain_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    """The append path reads under the same hold, and maps a damaged chain too.

    A harness's re-run reaches the store's read exactly as a decision does, so it
    is told what happened rather than handed the decoder's own error — and the
    damaged file is left as it is.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    torn = first.path.read_text(encoding="utf-8")[:120]
    first.path.write_text(torn, encoding="utf-8")

    with pytest.raises(UnreadableChain):
        store.append_version(
            first.path,
            value=_ANSWERS["minutes"] | {"body": "# Third"},
            actor=_HARNESS,
        )

    assert first.path.read_text(encoding="utf-8") == torn


def test_the_append_surface_answers_over_a_chain_damaged_after_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness re-running into a damaged chain is told what happened.

    The same sentence the decision path gives, and the same window is the only way
    there: the chain is damaged *after* the agent's read (the lookup a surface
    does) and before the store's read under the hold. The store-level test beside
    this one stops at :class:`UnreadableChain`; this one pins the surface, so
    dropping that mapping cannot pass the suite.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    first = _write(agent, "minutes")
    real = MeetingAgent.draft
    torn: dict[str, str] = {}

    def damage_after_the_read(self, draft_id):
        found = real(self, draft_id)
        if found is not None:
            torn["before"] = found.path.read_text(encoding="utf-8")
            found.path.write_text(torn["before"][:120], encoding="utf-8")
        return found

    monkeypatch.setattr(MeetingAgent, "draft", damage_after_the_read)

    with pytest.raises(MeetingAgentError, match="cannot be read"):
        agent.write(
            "minutes",
            _ANSWERS["minutes"] | {"body": "# Third"},
            draft_id=first.draft_id,
            actor=_HARNESS,
        )

    assert first.path.read_text(encoding="utf-8") == torn["before"][:120]


def test_two_writes_in_one_second_open_two_chains(tmp_path: Path) -> None:
    """A second write is a new chain, never an append to a chain by accident.

    The id is a digest of the facts, so two writes inside one clock second used
    to land on one directory — silently re-opening a decided draft.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    frozen = "2026-01-01T00:00:00+00:00"
    clock = lambda: frozen  # noqa: E731 - one frozen instant for both writes

    first = agent.write(
        "minutes",
        _ANSWERS["minutes"],
        clock=clock,
        actor=_HARNESS,
    )
    second = agent.write(
        "minutes",
        _ANSWERS["minutes"],
        clock=clock,
        actor=_HARNESS,
    )

    assert first.draft_id != second.draft_id
    assert len(agent.drafts()) == 2
    assert all(len(draft.versions) == 1 for draft in agent.drafts())


def test_a_chain_directory_that_has_not_been_written_yet_is_a_taken_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create is the claim on an id, not the file's existence.

    A writer that has created its chain's directory and not yet written its file
    is mid-write — the console and the MCP server are two processes — so the next
    writer inside the same clock second must take the next id rather than
    replacing the chain being written.
    """
    registry, meeting, _ = _workspace(tmp_path)
    agent = _agent(registry, meeting)
    frozen = "2026-01-01T00:00:00+00:00"
    clock = lambda: frozen  # noqa: E731 - one frozen instant for both writes

    # The first writer stops between creating its directory and writing it.
    real_write = store.write_draft
    monkeypatch.setattr(store, "write_draft", lambda draft: draft.path)
    first = agent.write(
        "minutes",
        _ANSWERS["minutes"],
        clock=clock,
        actor=_HARNESS,
    )
    monkeypatch.setattr(store, "write_draft", real_write)

    assert not (first.run_dir / DRAFT_FILENAME).exists()

    second = agent.write(
        "minutes",
        _ANSWERS["minutes"],
        clock=clock,
        actor=_HUMAN,
    )

    assert second.draft_id != first.draft_id
    assert second.run_dir != first.run_dir
    assert second.provenance.author == _HUMAN
    assert first.run_dir.is_dir()  # the mid-write chain's directory is left alone


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

    promoted = promote_draft(
        draft,
        registry=registry,
        meeting=meeting,
        version=draft.version,
        actor=_HUMAN,
    )

    assert promoted.review_state == "accepted"
    assert registry.latest_artifact(meeting.id, "minutes") is not None
