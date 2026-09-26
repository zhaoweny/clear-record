"""The audit record: the actor, what it touched, and that nothing rewrites it.

ADR-0033 decides the shape these tests hold to: **every mutating service call
appends one row** — ``(at, actor, action, target, outcome)`` — and the **actor is
a required argument** on the service's mutating entry points, so no surface can
forget it and no surface can forge it. The record is append-only: the schema
itself refuses a rewrite, not only the code that wrote the row.

What the *surfaces* supply is asserted where the surface is: the console and the
JSON API in ``tests/web/``, the stdio adapter in ``tests/mcp/``, the command
line in ``tests/cli/``. This module is the service's own half — the argument, the
row, the refusal, and the run whose ``origin`` and audit row are the same word.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from contextlib import closing

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from clear_record.service import (
    ACTORS,
    API,
    CLI,
    CONSOLE,
    MCP,
    QUEUE,
    RUN_ORIGINS,
    Meeting,
    MeetingAgent,
    MeetingAgentError,
    PipelineOptions,
    Registry,
    RunManager,
)


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting(registry: Registry, tmp_path, *, actor: str = CONSOLE) -> Meeting:
    registry.create_project("Ops", actor=actor)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace), actor=actor
    )


def _rows(registry: Registry, action: str):
    return [row for row in registry.list_audit_events() if row.action == action]


# --- the argument is required ----------------------------------------------- #


def test_a_mutating_service_call_must_name_its_actor(tmp_path) -> None:
    """Every mutating entry point refuses a call that names no actor.

    The argument has no default, so the failure is the method's own signature —
    not a row attributed to nobody, and not a write that happened anyway. This is
    what makes the record complete rather than a convention a surface can forget.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    recorded = len(registry.list_audit_events())

    calls: list[tuple[str, Callable[[], object]]] = [
        ("create_project", lambda: registry.create_project("No actor")),
        ("update_project", lambda: registry.update_project("ops")),
        ("add_term", lambda: registry.add_term("ops", "Falcon")),
        ("update_term", lambda: registry.update_term(1)),
        ("retire_term", lambda: registry.retire_term(1)),
        ("create_meeting", lambda: registry.create_meeting("ops", "No actor")),
        ("update_meeting", lambda: registry.update_meeting(meeting.id)),
        ("set_recording_set", lambda: registry.set_recording_set(meeting.id, ["a"])),
        ("set_meeting_workspace", lambda: registry.set_meeting_workspace(1, "/tmp/ws")),
        ("create_run", lambda: registry.create_run(meeting.id)),
        ("forget_tape", lambda: registry.forget_tape(1)),
        (
            "RunManager.start",
            lambda: RunManager(
                registry, pipeline=lambda *a, **k: None, start_queue=False
            ).start(meeting),
        ),
        (
            "MeetingAgent.write",
            lambda: MeetingAgent(registry, meeting).write("minutes", {"body": "x"}),
        ),
    ]
    for name, call in calls:
        with pytest.raises(TypeError):
            call()
        assert len(registry.list_audit_events()) == recorded, name

    assert registry.list_terms("ops") == []
    assert registry.list_runs(meeting.id) == []


def test_an_actor_outside_the_vocabulary_is_refused_before_anything_is_written(
    tmp_path,
) -> None:
    """A word the record cannot interpret is a refusal, not a row.

    The gate runs before the call does anything, so an unknown actor leaves no
    half-done work and no row nothing can read — and the vocabulary is one
    declaration (:data:`~clear_record.service.ACTORS`), not a convention each
    surface restates.
    """
    registry = _registry(tmp_path)

    with pytest.raises(ValueError, match="unknown actor"):
        registry.create_project("Ops", actor="hacker")

    assert registry.list_projects() == []
    assert registry.list_audit_events() == []
    assert set(ACTORS) == {CONSOLE, API, MCP, CLI, QUEUE}


# --- the row ----------------------------------------------------------------- #


def test_a_mutation_records_its_actor_its_target_and_its_outcome(tmp_path) -> None:
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    registry.add_term("ops", "Falcon", actor=CLI)
    registry.set_recording_set(meeting.id, ["a.wav"], actor=API)

    rows = registry.list_audit_events()

    assert [(row.actor, row.action, row.target, row.outcome) for row in rows] == [
        (CONSOLE, "project.create", "project:Ops", "ok"),
        (CONSOLE, "meeting.create", "meeting:Kickoff", "ok"),
        (CLI, "term.add", "term:Falcon", "ok"),
        (API, "meeting.tapes", f"meeting:{meeting.id}", "ok"),
    ]
    # The record is ordered as it was written: an append-only record's order is
    # the only ordering it can honestly have.
    assert [row.id for row in rows] == sorted(row.id for row in rows)


def test_a_refused_call_is_recorded_with_its_own_outcome(tmp_path) -> None:
    """A refusal is the row an audit record exists for, and it survives the rollback.

    The call's own write is rolled back with it, so the ``failed`` row is
    appended in a unit of work of its own — the record says an actor *tried* and
    was refused, and the registry is left exactly as it was.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops", slug="ops", actor=CONSOLE)

    with pytest.raises(ValueError, match="already exists"):
        registry.create_project("Again", slug="ops", actor=MCP)

    assert len(registry.list_projects()) == 1
    rows = _rows(registry, "project.create")
    assert [(row.actor, row.target, row.outcome) for row in rows] == [
        (CONSOLE, "project:ops", "ok"),
        (MCP, "project:ops", "failed"),
    ]


def test_a_row_outlives_what_it_names(tmp_path) -> None:
    """The row is the service's own account: it is not a foreign key.

    A tape that is forgotten is still accounted for — who registered it and who
    deleted it — which is the whole reason the table carries an address instead
    of a reference.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    tape = registry.register_tape(
        meeting.id, path="/tmp/a.wav", sha256="0" * 64, bytes=4, actor=CONSOLE
    )
    registry.forget_tape(tape.id, actor=API)

    assert registry.list_tapes(meeting.id) == []
    assert [
        (row.actor, row.action, row.target)
        for row in _rows(registry, "tape.register") + _rows(registry, "tape.forget")
    ] == [
        (CONSOLE, "tape.register", f"meeting:{meeting.id}"),
        (API, "tape.forget", f"tape:{tape.id}"),
    ]


# --- append-only ------------------------------------------------------------- #


def test_the_audit_record_refuses_every_rewrite(tmp_path) -> None:
    """No statement updates, deletes or *replaces* a row: the schema refuses all three.

    The service has no such call — :meth:`~clear_record.service.Registry.record_audit`
    appends and :meth:`~clear_record.service.Registry.list_audit_events` reads —
    and the database holds that against everything else that reaches it, which is
    what makes the record evidence rather than a column.

    ``REPLACE`` is the one that is easy to miss, and it needs **two** things to be
    refused: the triggers, and ``PRAGMA recursive_triggers = ON`` on the
    connection. SQLite fires a delete trigger on ``REPLACE``'s conflict path only
    with recursive triggers on — at the default it goes straight around both
    triggers and rewrites the row. So the rewrite is probed on a connection that
    was set up the way the registry sets up its own (``store._engine``), which is
    what the service and every caller of its engine get. A process that reaches
    the file with its own pragmas off is outside the app's reach entirely
    (ADR-0033's trust boundary: a same-uid process can rewrite the file, or the
    code), and the record does not claim otherwise.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops", actor=CONSOLE)
    original = registry.list_audit_events()

    with closing(sqlite3.connect(registry.db_path)) as conn:
        # A plain connection: no pragmas set, which is how everything but the
        # registry reaches the file. UPDATE and DELETE are refused by their own
        # triggers, and REPLACE by the insert-side one — the file-level guarantee,
        # which asks nothing of the connection.
        for statement in (
            "UPDATE audit_event SET actor = 'hacker'",
            "DELETE FROM audit_event",
            "INSERT OR REPLACE INTO audit_event (id, at, actor, action, target, outcome)"
            " VALUES (1, 'now', 'hacker', 'project.create', 'project:Ops', 'ok')",
            "REPLACE INTO audit_event (id, at, actor, action, target, outcome)"
            " VALUES (1, 'now', 'hacker', 'project.create', 'project:Ops', 'ok')",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)

    with registry._engine.connect() as conn:  # the registry's own connection setup
        assert conn.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 1
        for statement in (
            "INSERT OR REPLACE INTO audit_event (id, at, actor, action, target, outcome)"
            " VALUES (1, 'now', 'hacker', 'project.create', 'project:Ops', 'ok')",
            "REPLACE INTO audit_event (id, at, actor, action, target, outcome)"
            " VALUES (1, 'now', 'hacker', 'project.create', 'project:Ops', 'ok')",
        ):
            # SQLAlchemy's wrapper, not the driver's class: the registry raises
            # these (ADR-0030), and going through its engine is the point here.
            with pytest.raises(SAIntegrityError, match="append-only"):
                conn.exec_driver_sql(statement)

    # Every refused rewrite left the record exactly as it was.
    assert registry.list_audit_events() == original


# --- a run's origin is the actor its enqueue records ------------------------- #


def test_a_runs_origin_is_a_column_the_actor_is_the_transport(tmp_path) -> None:
    """Two words, two columns: what the run is attributed to, and who called.

    A run's ``origin`` says which surface the *run* belongs to and a run request
    may name it; the actor is the transport that carried the request. They are
    deliberately separate (ADR-0033): the command line asks a node for a run whose
    origin is ``cli``, and the call arrives — and is audited — as the node's API.
    Copying one into the other would let a client write itself into the record.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    registry.set_recording_set(meeting.id, ["a.wav"], actor=CONSOLE)
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)

    run = manager.start(meeting, PipelineOptions(), origin=CLI, actor=API)

    assert run.origin == CLI  # the run's own provenance, from the caller's word
    assert set(RUN_ORIGINS) == {CONSOLE, API, MCP, CLI}
    assert set(RUN_ORIGINS) <= set(ACTORS)
    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "run.enqueue")
    ] == [(API, "run.enqueue", f"meeting:{meeting.id}", "ok")]


def test_a_run_start_refusal_is_recorded_against_the_transport(tmp_path) -> None:
    """A guard refusal is the service's own answer, so it leaves a ``failed`` row.

    "No tape set" is a policy answer, not a key miss: the row names the meeting
    the start was refused for and the transport that asked (ADR-0033).
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)

    with pytest.raises(ValueError, match="no tape set"):
        manager.start(meeting, PipelineOptions(), origin=CONSOLE, actor=CONSOLE)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "run.enqueue")
    ] == [(CONSOLE, "run.enqueue", f"meeting:{meeting.id}", "failed")]


def test_a_draft_refusal_is_recorded_against_the_transport(tmp_path) -> None:
    """A stale decision is a policy answer: one ``failed`` row, and the chain whole."""
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    agent = MeetingAgent(registry, meeting)
    first = agent.write("minutes", {"body": "# One\n"}, actor=MCP)
    agent.write("minutes", {"body": "# Two\n"}, actor=MCP, draft_id=first.draft_id)

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.promote(first, actor=CONSOLE, version=1)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "draft.accept")
    ] == [(CONSOLE, "draft.accept", f"draft:{first.draft_id}", "failed")]
    assert agent.draft(first.draft_id).review_state == "draft"


def test_a_key_miss_is_not_a_row(tmp_path) -> None:
    """An unknown id is a lookup that found nothing, not a decision (ADR-0033).

    The line the record draws: a ``KeyError`` names nothing the service refused,
    so it appends nothing — the caller's 404 is the answer, and the record holds
    no row saying someone asked for something that does not exist.
    """
    registry = _registry(tmp_path)
    _meeting(registry, tmp_path)
    before = len(registry.list_audit_events())

    with pytest.raises(KeyError):
        registry.forget_tape(9999, actor=CONSOLE)
    with pytest.raises(KeyError):
        registry.update_term(9999, term="x", actor=CONSOLE)

    assert len(registry.list_audit_events()) == before


def test_a_bad_actor_is_refused_before_the_call_and_before_any_row(tmp_path) -> None:
    """``None``, an empty string and a non-string are not actors.

    The argument is required, so a caller that passes one of these named an actor
    the record cannot attribute a row to. Accepting ``None`` in particular used to
    skip the gate *and* the row — the mutation happened unaudited — so the gate
    reads the value and refuses it before the call runs (ADR-0033).
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    agent = MeetingAgent(registry, meeting)
    before = registry.list_audit_events()

    for bad in (None, "", "   ", 7, ["console"]):
        with pytest.raises(ValueError, match="unknown actor"):
            registry.create_project("Sneaky", actor=bad)
        with pytest.raises(ValueError, match="unknown actor"):
            agent.write("minutes", {"body": "# x\n"}, actor=bad)

    assert [project.slug for project in registry.list_projects()] == ["ops"]
    assert registry.list_audit_events() == before
    assert not list(
        meeting.workspace_path and Path(meeting.workspace_path).glob("agent/*")
    )


def test_a_draft_version_records_the_actor_its_transport_supplied(tmp_path) -> None:
    """A draft's author is the writer's actor, and the write is one audit row.

    There is no author a caller declares (ADR-0033 supersedes ADR-0031's declared
    identity), and the chain is a meeting's file the service owns, so writing one
    is recorded like any other mutation.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    agent = MeetingAgent(registry, meeting)

    draft = agent.write("minutes", {"body": "# Kickoff\n"}, actor=CLI)

    assert draft.provenance.author == CLI
    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "draft.write")
    ] == [(CLI, "draft.write", f"draft:{draft.draft_id}", "ok")]
