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

import contextlib
import inspect
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from contextlib import closing

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError as SAIntegrityError, OperationalError
from sqlalchemy.pool import NullPool

import clear_record.service.store as store_module
from clear_record.core import diagnostics
from clear_record.service import (
    ACTORS,
    API,
    CLI,
    CONSOLE,
    MCP,
    QUEUE,
    RUN_IN_FLIGHT,
    RUN_ORIGINS,
    Meeting,
    MeetingAgent,
    MeetingAgentError,
    PipelineOptions,
    Registry,
    RunManager,
    audit,
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


#: The `Registry` methods the record holds rows for, with the verb each one's rows
#: carry. This is the inventory the hand-kept call list above cannot be — and it is
#: checked against the class itself (``audit.recorded`` marks its wrapper), so a
#: method that gains or loses its decoration, or changes its action, fails here
#: rather than leaving a hole in the record that nothing notices. Two methods
#: share ``credential.set`` because they are the two halves of one statement
#: (insert, or replace), and the verb is the act either way.
_AUDITED_ACTIONS = {
    "add_archive": "archive.add",
    "add_artifact": "artifact.add",
    "add_run_event": "run.event",
    "add_term": "term.add",
    "claim_credential": "credential.set",
    "claim_run": "run.claim",
    "create_meeting": "meeting.create",
    "create_machine_token": "token.mint",
    "create_project": "project.create",
    "create_run": "run.enqueue",
    "fail_unreadable_run": "run.unreadable",
    "forget_tape": "tape.forget",
    "heartbeat_run": "run.heartbeat",
    "interrupt_run": "run.interrupt",
    "register_tape": "tape.register",
    "request_cancel": "run.cancel",
    "restore_term": "term.restore",
    "retain_artifact_paths": "artifact.retain",
    "retire_term": "term.retire",
    "revoke_machine_token": "token.revoke",
    "set_meeting_status": "meeting.status",
    "set_meeting_workspace": "meeting.workspace",
    "set_recording_set": "meeting.tapes",
    "stop_run": "run.stop",
    "store_credential": "credential.set",
    "update_meeting": "meeting.update",
    "update_project": "project.update",
    "update_run": "run.update",
    "update_term": "term.update",
}

#: The class's members that take an ``actor`` and are **not** recorded, with the
#: reason the record holds no row of their own. ``record_audit`` and
#: ``record_refusal`` *are* the append, and a registration composes two calls whose
#: rows are their own: each names an actor because every path into the record does.
_UNRECORDED_ACTOR_TAKERS = {
    "record_audit": "it is the append itself",
    "record_refusal": "the failed half of the same append",
    "meeting_for_workspace": "a registration: it composes create_project/create_meeting, whose own calls are the rows",
}

#: The class's **writers** that name no actor at all, and why the record has no row
#: for them — the audit module's own rule, stated where a new writer can be checked
#: against it. The session table's bookkeeping is not history of the project data
#: and one row per request would bury the record that is; a migration runs before
#: any record exists; and a token's *use* stamp answers nothing about who holds the
#: key (that is the mint, which is recorded).
#:
#: This is the one shape the class scan cannot see: a new method that writes and
#: takes neither an actor nor the decorator has nothing to notice it by, and the
#: reviewer is the check. What the scan does hold is the other two — a recorded
#: method with no *required* actor, and an actor-taking method that is not recorded.
_WRITERS_WITHOUT_AN_ACTOR = {
    "_migrate": "runs before the record exists",
    "create_session": "sign-in: session bookkeeping, not project history",
    "touch_session": "the idle clock's lazy write",
    "end_session": "sign-out",
    "end_all_sessions": "sign-out everywhere",
    "prune_expired_sessions": "the expired-session sweep",
    "touch_machine_token": "a token's use stamp: not who holds the key",
}


def _registry_methods() -> dict[str, Callable[..., object]]:
    """The class's own methods, by name — the inventory's source of truth."""
    return {
        name: member
        for name, member in inspect.getmembers(
            store_module.Registry, inspect.isfunction
        )
    }


def test_the_recorded_inventory_is_the_class_itself() -> None:
    """What is recorded, and as what, is read off the class — not kept beside it.

    ``audit.recorded`` marks the method it wrapped with the verb its rows carry, so
    the class answers both halves of the question a hand-kept list has to guess at:
    a mutating method that **forgot** the decorator is missing from the answer, and
    one that renamed its action changes the answer. The property that makes the
    argument's absence the method's own ``TypeError`` — required, not defaulted —
    is checked here too, for every entry at once.
    """
    methods = _registry_methods()
    recorded = {
        name: getattr(member, "audited_action")
        for name, member in methods.items()
        if hasattr(member, "audited_action")
    }

    assert recorded == _AUDITED_ACTIONS
    for name in recorded:
        actor = inspect.signature(methods[name]).parameters["actor"]
        assert actor.default is inspect.Parameter.empty, name


def test_every_recorded_registry_method_refuses_a_call_that_names_no_actor(
    tmp_path,
) -> None:
    """The same failure the list above drives, for **every** recorded method.

    The calls here are not realistic — every argument but the actor is bound to
    ``None`` — and that is the point: Python binds before it runs a body, so the
    missing actor is refused whatever the other values are, and the refusal is the
    one this test is about. (The realistic calls, with real ids and meeting, are
    the list at the top of this module: thirteen of these methods driven as a
    surface drives them.) A method whose ``actor`` gained a default would take
    ``None`` here and run — which is the weakening this catches, since the row
    would then be attributed to whatever word the default carried.
    """
    registry = _registry(tmp_path)
    before = len(registry.list_audit_events())

    for name, member in _registry_methods().items():
        if not hasattr(member, "audited_action"):
            continue
        parameters = inspect.signature(member).parameters
        junk = {
            key: None
            for key, parameter in parameters.items()
            if key != "actor"
            and key != "self"
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        with pytest.raises(TypeError, match="actor"):
            member(registry, **junk)
        assert len(registry.list_audit_events()) == before, name


def test_the_unrecorded_registry_methods_are_the_documented_ones() -> None:
    """The exceptions are named, so the inventory above is complete rather than partial.

    Two shapes, both stated with their reason: a method that takes an ``actor`` and
    records nothing itself, and a writer that names no actor because nothing about
    it is history of the project data. The first is checked as a **set** — every
    method of the class that takes an actor is recorded or named here, which is what
    catches a mutating method that forgot the decorator without forgetting the
    argument. The second is a list the reviewer checks a new writer against; a
    writer that takes neither is the one thing no scan can see (see the table).
    """
    methods = _registry_methods()
    takers = {
        name
        for name, member in methods.items()
        if "actor" in inspect.signature(member).parameters
    }

    assert takers == set(_AUDITED_ACTIONS) | set(_UNRECORDED_ACTOR_TAKERS)

    for name, reason in _WRITERS_WITHOUT_AN_ACTOR.items():
        member = methods[name]  # KeyError if one was renamed away
        assert "actor" not in inspect.signature(member).parameters, reason
        assert not hasattr(member, "audited_action"), reason


def test_a_recorded_method_that_takes_no_actor_is_refused_at_decoration_time() -> None:
    """The guard a new store method meets: decoration without an actor never imports.

    This is why the decorator is enough for "cannot be forgotten": a method that
    says it is audited and has no actor to attribute the row to stops the class
    body, so it cannot reach a caller — and the failure names the method and the
    reason rather than surfacing later as a row nobody can account for.
    """
    with pytest.raises(TypeError, match="takes no actor"):
        # The decoration happens here exactly as it does in the class body, and a
        # class body that raised here would never define the class at all.

        @audit.recorded("widget.update", "widget:name")
        def update_widget(self, name: str) -> None:  # pragma: no cover - never called
            raise AssertionError("the decorator let a method without an actor through")


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


def test_the_retire_and_the_restore_each_append_their_own_row(tmp_path) -> None:
    """Both halves of a reversible delete are in the record, under their actor.

    The machine API's ``DELETE`` on a glossary term **retires** rather than
    deleting, and the restore puts it back — two moves of one term, and each is
    a mutation the record accounts for on its own (ADR-0033). The target is the
    term's **id**: a restore addresses the row by id, and the pair reads as the
    history of that row. The actor is the surface that asked, so who retired it
    and who restored it are answerable separately.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops", actor=CONSOLE)
    term = registry.add_term("ops", "Falcon", actor=CONSOLE)

    registry.retire_term(term.id, actor=API)
    registry.restore_term(term.id, actor=CLI)

    rows = registry.list_audit_events()
    assert [(row.actor, row.action, row.target, row.outcome) for row in rows] == [
        (CONSOLE, "project.create", "project:Ops", "ok"),
        (CONSOLE, "term.add", "term:Falcon", "ok"),
        (API, "term.retire", f"term:{term.id}", "ok"),
        (CLI, "term.restore", f"term:{term.id}", "ok"),
    ]


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


def test_a_draft_write_refusal_is_recorded_against_the_transport(tmp_path) -> None:
    """A writer's own two answers are policy refusals, so each leaves its row.

    ``draft.write`` refuses above the store: a kind the service has no shape for,
    and a value whose shape its kind cannot use. Both are the service deciding
    against the write — not a key miss — so both are recorded, with the transport
    that carried the write as the actor and the chain (or the kind, when no chain
    exists yet) as the target. The chain is untouched either way.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    agent = MeetingAgent(registry, meeting)
    written = agent.write("minutes", {"body": "# One\n"}, actor=MCP)

    with pytest.raises(MeetingAgentError, match="unknown draft kind"):
        agent.write("widget", {"body": "x"}, actor=CLI)

    with pytest.raises(MeetingAgentError, match="cannot be written"):
        agent.write("minutes", {"nope": 1}, actor=API, draft_id=written.draft_id)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "draft.write")
    ] == [
        (MCP, "draft.write", f"draft:{written.draft_id}", "ok"),
        # A new chain that was refused names the kind it was asked for: there is
        # no draft id to name, and the target is read off the call's arguments.
        (CLI, "draft.write", "draft:widget", "failed"),
        (API, "draft.write", f"draft:{written.draft_id}", "failed"),
    ]
    assert agent.draft(written.draft_id).version == 1


def test_a_draft_reject_refusal_is_recorded_against_the_transport(tmp_path) -> None:
    """The reject half of a stale decision is a row too, under its own action.

    The accept path's twin: the decision names the version the reviewer read, a
    new version makes that stale, and the refusal says so in the record — one
    ``failed`` row, the same actor and target rules, and the chain left as it was.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    agent = MeetingAgent(registry, meeting)
    first = agent.write("minutes", {"body": "# One\n"}, actor=MCP)
    agent.write("minutes", {"body": "# Two\n"}, actor=MCP, draft_id=first.draft_id)

    with pytest.raises(MeetingAgentError, match="not the newest"):
        agent.reject(first, actor=CONSOLE, version=1)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "draft.reject")
    ] == [(CONSOLE, "draft.reject", f"draft:{first.draft_id}", "failed")]
    assert agent.draft(first.draft_id).review_state == "draft"


def test_a_resume_refusal_is_recorded_against_the_transport(tmp_path) -> None:
    """``run.resume``'s guard is a policy answer, so it leaves a ``failed`` row.

    A resume of a run that is still in flight (or one whose options cannot be
    reconstructed) is the service deciding against the call, not a key miss: the
    row names the run it was about and the transport that asked, and the refusal
    reaches the caller unchanged. It is a different entry point from ``start``,
    with its own action, and it was the one refusal on the run edges with no row
    of its own asserted.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    registry.set_recording_set(meeting.id, ["a.wav"], actor=CONSOLE)
    in_flight = registry.create_run(meeting.id, origin=CONSOLE, actor=CONSOLE)
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)

    with pytest.raises(ValueError, match="still in flight"):
        manager.resume(in_flight.id, origin=API, actor=API)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in _rows(registry, "run.resume")
    ] == [(API, "run.resume", f"run:{in_flight.id}", "failed")]


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


def test_a_retention_that_moved_nothing_is_not_a_row(tmp_path) -> None:
    """The publication that preserved nothing has no ``artifact.retain`` row.

    ``retain_artifact_paths`` is the conditional write a publication makes when
    it replaces a root document some run scope does not hold: it answers with the
    number of rows it moved, and that number is the statement's own "no row
    moved" when it is zero — an empty mapping, or a mapping whose paths no
    artifact row names. The answer has to be the shape the record reads
    (``None``/``False``), not a count the caller has to interpret: a ``0`` read as
    a moved row appends an ``ok`` row for a publication that preserved nothing,
    which says the service did something it did not.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    before = len(registry.list_audit_events())

    assert registry.retain_artifact_paths(meeting.id, {}, actor=QUEUE) is None
    assert (
        registry.retain_artifact_paths(
            meeting.id,
            {str(tmp_path / "nothing-names-this.json"): str(tmp_path / "copy.json")},
            actor=QUEUE,
        )
        is None
    )

    assert len(registry.list_audit_events()) == before
    assert _rows(registry, "artifact.retain") == []


def test_a_conditional_write_that_moved_nothing_records_nothing(tmp_path) -> None:
    """A run-lifecycle statement that matched no row is not recorded as done.

    ``claim_run``, ``heartbeat_run``, ``stop_run``, ``request_cancel``,
    ``interrupt_run`` and ``fail_unreadable_run`` answer a conditional write that
    moved no row with ``None`` or ``False`` — a lost claim, a run that is not
    running, a row already terminal, an id that names no row at all. None of
    those is a mutation the record may say happened, and an unknown id names no
    subject to write about, so each appends nothing (ADR-0033). The row is the
    statement's own answer, never the assumption that the call acted.
    """
    registry = _registry(tmp_path)
    meeting = _meeting(registry, tmp_path)
    registry.set_recording_set(meeting.id, ["a.wav"], actor=CONSOLE)
    run = registry.create_run(meeting.id, origin=CONSOLE, actor=CONSOLE)
    claimed = registry.claim_run(run.id, owner="node:1", actor=QUEUE)
    assert claimed is not None and claimed.status == "running"

    before = len(registry.list_audit_events())
    # A lost claim: another claimant already took the row.
    assert registry.claim_run(run.id, owner="node:2", actor=QUEUE) is None
    # A row that exists but is not in the state the move is legal from.
    assert registry.stop_run(run.id, ended_at="now", progress={}, actor=QUEUE) is None
    # Ids that name no row at all: no subject, so no row either.
    assert registry.claim_run(9999, owner="node:1", actor=QUEUE) is None
    assert registry.heartbeat_run(9999, actor=QUEUE) is False
    assert registry.request_cancel(9999, actor=QUEUE) is None
    assert registry.stop_run(9999, ended_at="now", progress={}, actor=QUEUE) is None
    assert (
        registry.interrupt_run(
            9999,
            actor=QUEUE,
            observed=claimed,
            ended_at="now",
            error="gone",
            progress={},
        )
        is None
    )
    assert registry.fail_unreadable_run(9999, actor=QUEUE, error="gone") is False

    assert len(registry.list_audit_events()) == before
    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in registry.list_audit_events()
        if row.action
        in {
            "run.claim",
            "run.heartbeat",
            "run.stop",
            "run.cancel",
            "run.interrupt",
            "run.unreadable",
        }
    ] == [(QUEUE, "run.claim", f"run:{run.id}", "ok")]


# --- the record under contention --------------------------------------------- #
#
# ADR-0033's promise is a row per mutating call, and the half of it that may
# never give way is the *call's own outcome*: a refused call answers with the
# service's refusal, never with a database error from the record of it (which is
# what an HTTP surface would turn into a 500 where the spec says 409). The shape
# that puts the two in conflict is the one this store's own write-ahead-log
# rationale calls ordinary: another surface holding the registry's write lock
# past the connection's busy timeout.

#: The busy timeout these tests' engines carry. A held lock costs the driver's
#: five seconds *per wait*, and what is under test is how many waits a call
#: spends — so the budget is short enough to wait out several times over and
#: keep the suite quick. The shape is ``test_store``'s
#: (:func:`test_store._engine_timing_out_box`), with the connection state the
#: registry's own engine sets.
_BUSY_TIMEOUT = 0.5


def _timing_out_engine(path: Path, timeout: float = _BUSY_TIMEOUT):
    """An engine like the registry's own, with a busy timeout a test can wait out.

    The budget is the *test's*, set through the driver's own ``timeout`` connect
    argument — which is the knob the registry's ``PRAGMA busy_timeout`` sets on
    its own connections — so this box deliberately does not set that pragma: a
    test that shortened the budget only to have the policy write it back would
    wait five seconds for a decision it is asserting. Nothing here converts a
    journal mode either: that is the file's, decided once as a registry opens
    (``store._write_ahead_log``).
    """
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        poolclass=NullPool,
        connect_args={"timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _connection_state(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA recursive_triggers = ON")

    return engine


def _release(holder: sqlite3.Connection) -> None:
    """Give the lock back; a connection already released is not an error."""
    try:
        holder.rollback()
        holder.close()
    except sqlite3.ProgrammingError:  # a second release (the timer's, or ours)
        pass


@contextlib.contextmanager
def _held_write_lock(
    registry: Registry, *, released_after: float | None = None
) -> Iterator[sqlite3.Connection]:
    """Another surface's write lock, held for the block — or released on a timer.

    ``BEGIN IMMEDIATE`` takes the lock and keeps it; the statement after it
    writes a name as its own name, so what the connection holds is the lock and
    nothing else. The connection is made thread-safe because ``released_after``
    hands the release to a timer, which is the one shape that lets the call below
    *wait a lock out* instead of meeting it.
    """
    holder = sqlite3.connect(str(registry.db_path), timeout=0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE project SET name = name WHERE id = 1")
    timer = (
        threading.Timer(released_after, _release, args=(holder,))
        if released_after is not None
        else None
    )
    if timer is not None:
        timer.start()
    try:
        yield holder
    finally:
        if timer is not None:
            timer.join(timeout=10)
        _release(holder)


def _a_run_in_flight(registry: Registry, tmp_path) -> Meeting:
    """A meeting with a tape set and a live run: whatever starts next is refused."""
    meeting = _meeting(registry, tmp_path)
    registry.set_recording_set(meeting.id, ["a.wav"], actor=CONSOLE)
    registry.create_run(meeting.id, origin=CLI, actor=CONSOLE)
    return meeting


def _lost_rows() -> list[dict]:
    """Every ``audit.row_lost`` record the node's log holds, oldest first."""
    try:
        lines = diagnostics.log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [
        record
        for record in (json.loads(line) for line in lines if line.strip())
        if record.get("event") == "audit.row_lost"
    ]


def test_a_refusal_under_a_held_write_lock_is_still_the_refusal(
    tmp_path, monkeypatch
) -> None:
    """The record of a refusal may not replace it: the ``ValueError``, one wait, a stated loss.

    The reproduction: another surface holds the registry's write lock past the
    busy timeout while a second ``RunManager.start`` is refused for a run already
    in flight — one of the four refusals the audit docstring names as exactly the
    rows an audit record exists for. At the base the append paid the busy timeout
    *inside the failure handler* and let the driver's ``OperationalError`` out as
    the caller's answer: zero ``failed`` rows, and a 500 at the route whose spec
    says 409. The caller now keeps the service's own refusal, the append waits
    the busy timeout at most once (this call has spent none of it: the guard is a
    read, and the record's log is a log), and the row the registry would not take
    is stated as ``audit.row_lost`` instead of raised.
    """
    monkeypatch.setattr(store_module, "_engine", _timing_out_engine)
    registry = _registry(tmp_path)
    meeting = _a_run_in_flight(registry, tmp_path)
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)
    before = len(registry.list_audit_events())

    with _held_write_lock(registry):
        started = time.monotonic()
        with pytest.raises(ValueError, match=RUN_IN_FLIGHT):
            manager.start(meeting, origin=CLI, actor=API)
        waited = time.monotonic() - started

    assert waited < 2 * _BUSY_TIMEOUT, "the append paid the busy timeout twice"
    assert len(registry.list_audit_events()) == before  # the lock was held throughout
    assert [
        (row["actor"], row["action"], row["target"], row["outcome"])
        for row in _lost_rows()
    ] == [(API, "run.enqueue", f"meeting:{meeting.id}", "failed")]


def test_a_refusals_row_lands_when_the_lock_lets_go_inside_the_budget(
    tmp_path, monkeypatch
) -> None:
    """Tolerating the lock is not giving up the row: a wait inside the budget still writes it.

    The lock is released while the append is waiting for it — the ordinary shape
    of two surfaces sharing one registry — so the ``failed`` row lands and
    nothing was lost to state. Without this half, "tolerate a locked registry"
    could be read as "never write the row under contention", which is a weaker
    record than the one ADR-0033 asks for.
    """
    monkeypatch.setattr(store_module, "_engine", _timing_out_engine)
    registry = _registry(tmp_path)
    meeting = _a_run_in_flight(registry, tmp_path)
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)
    before = len(registry.list_audit_events())

    with _held_write_lock(registry, released_after=_BUSY_TIMEOUT / 5):
        with pytest.raises(ValueError, match=RUN_IN_FLIGHT):
            manager.start(meeting, origin=CLI, actor=API)

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in registry.list_audit_events()[before:]
    ] == [(API, "run.enqueue", f"meeting:{meeting.id}", "failed")]
    assert _lost_rows() == []


def test_a_store_refusal_under_a_held_write_lock_is_still_the_refusal(
    tmp_path, monkeypatch
) -> None:
    """The same line one layer down: the policy's ``ValueError``, not the driver's error.

    A store method's own policy answer (here the status vocabulary) is raised
    before any write, so the lock costs nothing until the *record* of the refusal
    is attempted. The refusal is the caller's answer either way (ADR-0033's
    ``failed`` row belongs to the record, not to the answer), and the row it
    could not write is stated.
    """
    monkeypatch.setattr(store_module, "_engine", _timing_out_engine)
    registry = _registry(tmp_path)
    registry.create_project("Ops", actor=CONSOLE)

    with _held_write_lock(registry):
        started = time.monotonic()
        with pytest.raises(ValueError, match="status must be one of"):
            registry.add_term("ops", "Falcon", status="not-a-status", actor=CONSOLE)
        waited = time.monotonic() - started

    assert waited < 2 * _BUSY_TIMEOUT
    assert _rows(registry, "term.add") == []
    assert [
        (row["actor"], row["action"], row["target"], row["outcome"])
        for row in _lost_rows()
    ] == [(CONSOLE, "term.add", "term:Falcon", "failed")]


def test_a_failure_the_registry_itself_refused_costs_one_busy_timeout(
    tmp_path, monkeypatch
) -> None:
    """A write the registry refused is not retried on the record's behalf.

    The mutation's own insert waited the busy timeout out and was refused; an
    append then would pay that same timeout again for the same refusal, which is
    the double wait the base performed (two budgets, one call). The budget is
    spent once, and the refusal the loss *states* is the call's own — the
    distinguishing evidence, since a second attempt would have failed on the
    audit row's insert and stated that instead. The budget here is one second so
    that the two shapes are a second apart rather than half of one.
    """
    monkeypatch.setattr(
        store_module, "_engine", lambda path: _timing_out_engine(path, 1.0)
    )
    registry = _registry(tmp_path)

    with _held_write_lock(registry):
        started = time.monotonic()
        with pytest.raises(OperationalError):
            registry.create_project("Second", actor=CONSOLE)
        waited = time.monotonic() - started

    assert waited < 1.5, "the busy timeout was paid twice for one call"
    assert _rows(registry, "project.create") == []
    lost = _lost_rows()
    assert [
        (row["actor"], row["action"], row["target"], row["outcome"]) for row in lost
    ] == [(CONSOLE, "project.create", "project:Second", "failed")]
    assert "audit_event" not in lost[0]["reason"], (
        "the stated loss names a second attempt at the row, not the call's own refusal"
    )
