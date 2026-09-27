"""Registry behaviour and its schema's history.

Projects and the multi-project glossary table, and the revisions a registry
migrates through when it opens: the schema the Alembic revisions own, the
mapping that mirrors it, the released line's ladder row that lets that line's
registry be placed at its own baseline, and the one-active-run index revision
0009 puts over a meeting. These exercise the service seam directly (external
behaviour, temp DB — no web app, no network), so the store is trusted
independently of any adapter.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import URL, UniqueConstraint, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from clear_record.core import JobEvent, PipelineOptions, diagnostics
from clear_record.core.paths import registry_path
from clear_record.service import (
    RUN_ORIGINS,
    RUN_STATUSES,
    Registry,
    RegistryLocked,
    entities,
)
from clear_record.service import models as models_module
from clear_record.service import store as store_module
from clear_record.service.lifecycle import active_run_predicate
from clear_record.service.store import (
    _LADDER_VERSION,
    _alembic_config,
)

#: The repository root: the tree this test's git history lives in.
_REPO = Path(__file__).resolve().parents[4]

#: Where the revisions are, for the tests that copy the chain to a temp tree.
_MIGRATIONS = Path(store_module.__file__).resolve().parent / "migrations"

#: The released baseline: the schema the released line (`v0.2.0`) shipped, which
#: is the revision the chain begins at — and the number that line recorded its
#: schema as. A fixture that manufactures an "old" registry pins **this**, never
#: another ladder step: 6 is the one step a supported registry stands at, so a
#: fixture pinned at any other would be testing a shape the shim refuses.
_BASELINE = "0006"
_BASELINE_LADDER = 6


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _registry_at(db_path: Path, revision: str) -> None:
    """Build a registry at one revision of its schema's history.

    A registry an *older build* left is made the way this build makes one — the
    chain is the schema's history — so ``revision`` is the released baseline for
    every fixture that manufactures an old registry (see :data:`_BASELINE`), and
    a delta's revision for the ones that need a registry without it.
    """
    command.upgrade(_alembic_config(db_path), revision)


def _ladder_left_it(db_path: Path, version: int) -> None:
    """Give a registry the shape the retired ladder left behind at one version.

    The ladder kept its version in ``schema_version`` and knew nothing of
    Alembic, so a registry from the released line before this build looks like
    this — and a registry from a *development* build looks the same with any
    other number in the row, which is the difference the shim reads.
    """
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _version_tables(db_path: Path) -> list[str]:
    """Which version states a registry carries: the ladder's and Alembic's."""
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        return sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name IN ('schema_version', 'alembic_version')"
            )
        )


def _normalized(sql: str | None) -> str | None:
    """One SQL fragment with its whitespace collapsed, for comparing two spellings."""
    return " ".join(sql.split()) if sql is not None else None


def _index_predicate(ddl: str | None) -> str | None:
    """The built index's own ``WHERE`` clause, normalized (``None`` if it has none).

    What SQLite stores is the ``CREATE INDEX`` statement the revision ran, so the
    predicate is read back out of the DDL itself rather than described a second
    time here.
    """
    if ddl is None:
        return None
    match = re.search(r"\bWHERE\b(.*)$", ddl, re.IGNORECASE | re.DOTALL)
    return _normalized(match.group(1)) if match else None


def _index_sql(db_path: Path, name: str) -> str | None:
    """A built index's DDL: the ``CREATE INDEX`` statement, normalized."""
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?", (name,)
        ).fetchone()
    return _normalized(row[0]) if row else None


def _seed_project(db_path: Path) -> None:
    """A project row as an existing registry would already have one."""
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )


def test_create_and_list_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    project = reg.create_project(
        "Weekly Ops",
        notes="ops sync",
        actor="console",
    )

    assert project.slug == "weekly-ops"
    assert project.name == "Weekly Ops"
    assert project.notes == "ops sync"
    assert reg.list_projects() == [project]
    assert reg.get_project("weekly-ops") == project
    assert reg.get_project("nope") is None


def test_slug_collision_gets_a_suffix(tmp_path) -> None:
    reg = _registry(tmp_path)
    assert (
        reg.create_project(
            "Sync",
            actor="console",
        ).slug,
        reg.create_project(
            "Sync",
            actor="console",
        ).slug,
    ) == (
        "sync",
        "sync-2",
    )


def test_duplicate_explicit_slug_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "A",
        slug="shared",
        actor="console",
    )
    with pytest.raises(ValueError):
        reg.create_project(
            "B",
            slug="shared",
            actor="console",
        )


def test_unsafe_explicit_slugs_are_rejected(tmp_path) -> None:
    """A caller-supplied slug must be [a-z0-9-]+; the managed root builds paths from it."""
    reg = _registry(tmp_path)
    for bad in ("../escaped", "a/b", "UPPER", "with space", "."):
        with pytest.raises(ValueError, match="slug must match"):
            reg.create_project(
                "Ops",
                slug=bad,
                actor="console",
            )

    reg.create_project(
        "Ops",
        actor="console",
    )
    with pytest.raises(ValueError, match="slug must match"):
        reg.create_meeting(
            "ops",
            "Kickoff",
            slug="../escaped",
            actor="console",
        )


def test_blank_project_name_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(ValueError):
        reg.create_project(
            "   ",
            actor="console",
        )


def test_state_survives_reopen(tmp_path) -> None:
    db = tmp_path / "registry.sqlite3"
    Registry(db).create_project("Persist", actor="console")
    assert [p.slug for p in Registry(db).list_projects()] == ["persist"]


def test_update_project(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    updated = reg.update_project(
        "ops",
        name="Ops Weekly",
        notes="n",
        actor="console",
    )
    assert (updated.name, updated.notes) == ("Ops Weekly", "n")
    with pytest.raises(KeyError):
        reg.update_project(
            "missing",
            name="x",
            actor="console",
        )


def test_glossary_lifecycle(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    term = reg.add_term(
        "ops",
        "李工",
        reading="Li Gong",
        aliases="老李",
        definition="lead",
        actor="console",
    )
    assert term.status == "candidate" and term.added_by == "human"
    assert reg.list_terms("ops") == [term]

    assert (
        reg.update_term(
            term.id,
            status="confirmed",
            actor="console",
        ).status
        == "confirmed"
    )
    assert (
        reg.update_term(
            term.id,
            definition="team lead",
            actor="console",
        ).definition
        == "team lead"
    )

    retired = reg.retire_term(term.id, actor="console")
    assert retired.status == "retired"
    # A retire is a status change, not a row delete: the history survives.
    assert retired.added_by == "human" and retired.created_at
    assert reg.list_terms("ops") == [retired]
    assert reg.list_terms("ops", status="confirmed") == []

    assert (
        reg.update_term(term.id, status="confirmed", actor="console").status
        == "confirmed"
    )
    with pytest.raises(KeyError):
        reg.retire_term(9999, actor="console")


def test_retire_records_the_status_a_restore_returns_the_term_to(tmp_path) -> None:
    """A retire is reversible to where the term was, not to "confirmed"."""
    reg = _registry(tmp_path)
    reg.create_project("Ops", actor="console")
    draft = reg.add_term("ops", "AgentTerm", added_by="agent", actor="console")
    owner = reg.add_term("ops", "Falcon", status="confirmed", actor="console")
    noted = reg.add_term(
        "ops", "Mars", status="confirmed", notes="call it Mars", actor="console"
    )

    for term in (draft, owner, noted):
        assert reg.retire_term(term.id, actor="console").status == "retired"
    # The marker a retire records stays inside the registry: it is never the
    # term's published notes.
    assert reg.get_term(noted.id).notes == "call it Mars"

    # Un-reviewed, still; then the status the retire took each term from.
    assert reg.restore_term(draft.id, actor="console").status == "candidate"
    assert reg.restore_term(owner.id, actor="console").status == "confirmed"
    restored = reg.restore_term(noted.id, actor="console")
    assert (restored.status, restored.notes) == ("confirmed", "call it Mars")
    assert reg.list_terms("ops", status="retired") == []


def test_a_status_change_to_retired_records_where_it_came_from(tmp_path) -> None:
    """The status control retires like the verb: the marker is written on it.

    ``update_term(status="retired")`` is the console's status control and the
    API's ``PATCH``, and it must leave the term exactly as ``retire_term`` does —
    the row survives, and the status it came from is recorded on it — so a
    Restore returns a previously-confirmed term as **confirmed**, never as a
    candidate that silently leaves the decoder's bias.
    """
    reg = _registry(tmp_path)
    reg.create_project("Ops", actor="console")
    term = reg.add_term(
        "ops", "Falcon", status="confirmed", added_by="human", actor="console"
    )

    retired = reg.update_term(term.id, status="retired", actor="console")
    assert retired.status == "retired"
    assert reg.get_term(term.id).notes is None  # the marker is never published

    assert reg.restore_term(term.id, actor="console").status == "confirmed"


def test_restoring_a_term_that_is_not_retired_is_refused(tmp_path) -> None:
    """Only a retired term has a prior status to return to.

    Restoring a never-retired one would invent a status — a confirmed term would
    come back a candidate, dropping owner-accepted truth out of the decoder's
    bias — so the move is refused with the status the term actually holds, and
    the term is left exactly as it was.
    """
    reg = _registry(tmp_path)
    reg.create_project("Ops", actor="console")
    term = reg.add_term(
        "ops", "Falcon", status="confirmed", added_by="human", actor="console"
    )

    with pytest.raises(ValueError, match="not retired"):
        reg.restore_term(term.id, actor="console")

    assert reg.get_term(term.id).status == "confirmed"


def test_duplicate_term_in_one_project_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    reg.add_term(
        "ops",
        "Falcon",
        actor="console",
    )
    with pytest.raises(ValueError):
        reg.add_term(
            "ops",
            "Falcon",
            actor="console",
        )


def test_same_term_is_allowed_in_two_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    reg.create_project(
        "Research",
        actor="console",
    )
    reg.add_term(
        "ops",
        "Falcon",
        actor="console",
    )
    reg.add_term(
        "research",
        "Falcon",
        actor="console",
    )
    assert len(reg.list_terms()) == 2


def test_cross_project_table_filters_by_status_and_project(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    reg.create_project(
        "Research",
        actor="console",
    )
    reg.add_term(
        "ops",
        "Alpha",
        status="confirmed",
        actor="console",
    )
    reg.add_term(
        "ops",
        "Beta",
        status="candidate",
        actor="console",
    )
    reg.add_term(
        "research",
        "Gamma",
        status="confirmed",
        actor="console",
    )

    assert [t.term for t in reg.list_terms(status="confirmed")] == ["Alpha", "Gamma"]
    assert [t.term for t in reg.list_terms("ops")] == ["Alpha", "Beta"]
    assert [t.term for t in reg.list_terms("ops", status="confirmed")] == ["Alpha"]
    assert {t.project_slug for t in reg.list_terms()} == {"ops", "research"}


def test_term_counts_include_empty_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    reg.create_project(
        "Research",
        actor="console",
    )
    reg.add_term(
        "ops",
        "A",
        actor="console",
    )
    reg.add_term(
        "ops",
        "B",
        actor="console",
    )
    assert reg.term_counts() == {"ops": 2, "research": 0}


def test_invalid_status_and_author_are_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    with pytest.raises(ValueError):
        reg.add_term(
            "ops",
            "A",
            status="maybe",
            actor="console",
        )
    with pytest.raises(ValueError):
        reg.add_term(
            "ops",
            "A",
            added_by="robot",
            actor="console",
        )
    with pytest.raises(KeyError):
        reg.add_term(
            "nope",
            "A",
            actor="console",
        )


def test_agent_terms_are_marked_as_such(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    term = reg.add_term(
        "ops",
        "Falcon",
        added_by="agent",
        actor="console",
    )
    assert term.added_by == "agent"
    assert term.status == "candidate"


def test_meeting_lifecycle_and_tape_set(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        recorded_at="2026-09-14",
        actor="console",
    )
    assert meeting.slug == "kickoff"
    assert meeting.status == "new"
    assert meeting.notes == ""
    assert reg.list_meetings("ops") == [meeting]

    assert (
        reg.create_meeting(
            "ops",
            "Kickoff",
            actor="console",
        ).slug
        == "kickoff-2"
    )
    assert reg.get_meeting("ops", "kickoff") == meeting
    assert reg.get_meeting("ops", "nope") is None

    selected = reg.set_recording_set(
        meeting.id,
        ["/a.wav", "/b.wav"],
        actor="console",
    )
    assert selected.paths == ("/a.wav", "/b.wav")
    assert reg.latest_recording_set(meeting.id) == selected

    # A newer selection supersedes the old one.
    newer = reg.set_recording_set(
        meeting.id,
        ["/c.wav"],
        actor="console",
    )
    assert reg.latest_recording_set(meeting.id) == newer

    with pytest.raises(ValueError):
        reg.set_recording_set(
            meeting.id,
            [],
            actor="console",
        )


def test_update_meeting_notes_and_title(tmp_path) -> None:
    """The story has a durable home: meeting notes (and title) are writable."""
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )

    updated = reg.update_meeting(
        meeting.id,
        notes="tell the story here",
        actor="console",
    )
    assert updated.notes == "tell the story here"
    assert reg.get_meeting("ops", "kickoff").notes == "tell the story here"

    assert (
        reg.update_meeting(
            meeting.id,
            notes="",
            actor="console",
        ).notes
        == ""
    )
    assert (
        reg.update_meeting(
            meeting.id,
            title="Kickoff v2",
            actor="console",
        ).title
        == "Kickoff v2"
    )

    with pytest.raises(ValueError):
        reg.update_meeting(
            meeting.id,
            title="   ",
            actor="console",
        )
    with pytest.raises(KeyError):
        reg.update_meeting(
            999,
            notes="nope",
            actor="console",
        )


def test_run_and_artifact_rows(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(tmp_path),
        actor="console",
    )

    run = reg.create_run(
        meeting.id,
        backend="apple",
        model="small",
        actor="console",
    )
    assert run.status == "queued"
    assert reg.get_run(run.id) == run
    assert [r.id for r in reg.list_runs(meeting.id)] == [run.id]

    updated = reg.update_run(
        run.id,
        status="running",
        started_at="now",
        actor="console",
    )
    assert updated.status == "running"
    with pytest.raises(ValueError):
        reg.update_run(
            run.id,
            status="bogus",
            actor="console",
        )

    # RUN-02: origin is one of the four surfaces, and only a start path has one.
    assert run.origin is None
    with pytest.raises(ValueError):
        reg.create_run(
            meeting.id,
            origin="grafana",
            actor="console",
        )
    # A meeting carries one active run (revision 0009's index), so the run the
    # origin is read back from is the meeting's next one.
    reg.update_run(
        run.id,
        status="done",
        actor="console",
    )
    assert (
        reg.create_run(
            meeting.id,
            origin="cli",
            actor="console",
        ).origin
        == "cli"
    )

    artifact = reg.add_artifact(
        meeting.id,
        run_id=run.id,
        kind="record",
        path="/ws/record.json",
        sha256="ab",
        actor="console",
    )
    assert artifact.kind == "record"
    assert artifact.produced_by == "pipeline"
    assert reg.list_artifacts(meeting.id) == [artifact]

    assert (
        reg.set_meeting_status(
            meeting.id,
            "recorded",
            actor="console",
        ).status
        == "recorded"
    )
    with pytest.raises(ValueError):
        reg.set_meeting_status(
            meeting.id,
            "bogus",
            actor="console",
        )


def test_a_registry_at_the_released_baseline_gains_every_delta(tmp_path) -> None:
    """An existing registry at the released baseline gains every delta on it.

    The baseline is the schema a released install has, so this is the registry an
    upgrading user arrives with, holding their projects and meetings; the deltas
    on top of it must all answer, and the rows it already held must survive them.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _seed_project(db)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(
            "INSERT INTO meeting (project_id, slug, title, status, created_at)"
            " VALUES (1, 'kickoff', 'Kickoff', 'new', 'now')"
        )

    reg = Registry(db)
    assert [p.slug for p in reg.list_projects()] == ["ops"]
    meeting = reg.get_meeting("ops", "kickoff")
    assert meeting is not None and meeting.notes == ""
    assert (
        reg.update_meeting(
            meeting.id,
            notes="story",
            actor="console",
        ).notes
        == "story"
    )
    # The baseline's own shapes answer over the migrated registry: an uploaded
    # tape joins the meeting's tape set, and a run carries its durable options.
    tape = reg.register_tape(
        meeting.id,
        path="/tapes/a.wav",
        sha256="0" * 64,
        bytes=3,
        actor="console",
    )
    assert reg.list_tapes(meeting.id) == [tape]
    assert reg.latest_recording_set(meeting.id).paths == ("/tapes/a.wav",)
    run = reg.create_run(
        meeting.id,
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
        actor="console",
    )
    assert reg.get_run(run.id).run_options == dataclasses.asdict(
        PipelineOptions(backend="apple")
    )
    assert reg.count_run_events(run.id) == 0
    # Delta 0007: a run records where it came from, and the claim records its
    # owner. The claim runs on a *second* meeting's run: one meeting has one
    # active run, which revision 0009's index enforces.
    assert reg.get_run(run.id).origin is None  # a seeded row has no origin
    second = reg.create_meeting(
        "ops",
        "Second pass",
        actor="console",
    )
    queued = reg.create_run(
        second.id,
        origin="console",
        actor="console",
    )
    claimed = reg.claim_run(
        queued.id,
        owner="peer:1",
        actor="console",
    )
    assert claimed is not None
    assert (claimed.origin, claimed.owner) == ("console", "peer:1")
    assert claimed.heartbeat_at is not None
    # Delta 0008: a run can be linked to the run it resumes, and carry a cancel request.
    assert claimed.resumes_run_id is None and claimed.cancel_requested_at is None
    with pytest.raises(KeyError):
        reg.create_run(
            meeting.id,
            resumes_run_id=999,
            actor="console",
        )  # no such run
    assert (
        reg.request_cancel(
            claimed.id,
            actor="console",
        ).cancel_requested_at
        is not None
    )
    assert reg.cancel_requested(claimed.id) is True
    # A queued run is cancelled outright: it never reaches a pipeline.
    stopped = reg.stop_run(
        run.id,
        ended_at="now",
        progress={},
        actor="console",
    )
    assert stopped is not None and stopped.status == "stopped"
    # And a run that is not queued any more is left to its owner.
    assert (
        reg.stop_run(
            claimed.id,
            ended_at="now",
            progress={},
            actor="console",
        )
        is None
    )
    # Delta 0009: the meeting's run has ended, so the meeting runs again — and
    # the new run continues the stopped one.
    resumed = reg.create_run(
        meeting.id,
        resumes_run_id=run.id,
        actor="console",
    )
    assert resumed.resumes_run_id == run.id


def test_a_registry_from_the_retired_ladder_migrates_on_open(tmp_path) -> None:
    """A registry the released line left behind opens, keeps its rows, and is current.

    This is the registry an upgrading user has: the released line's tables with
    its ``schema_version`` row and no revision recorded. Opening it must place it
    at the baseline that row names — not re-run the schema it already carries —
    and run the deltas on top.

    The row carries the **released** line's number (``v0.2.x`` stopped at 6, the
    schema this chain's ``0006`` is), which is the only number the placement
    accepts; every other ladder step is a development build's and is refused —
    see the test below.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _ladder_left_it(db, _BASELINE_LADDER)
    _seed_project(db)

    reg = Registry(db)
    assert [p.slug for p in reg.list_projects()] == ["ops"]
    # Every delta is in place: a run's ownership (0007), its cancel and resume
    # columns (0008) and the one-active-run index (0009) all answer.
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )
    assert (
        reg.update_meeting(
            meeting.id,
            notes="story",
            actor="console",
        ).notes
        == "story"
    )
    tape = reg.register_tape(
        meeting.id,
        path="/tapes/a.wav",
        sha256="0" * 64,
        bytes=3,
        actor="console",
    )
    assert reg.list_tapes(meeting.id) == [tape]
    assert (
        reg.create_run(
            meeting.id,
            origin="cli",
            actor="console",
        ).origin
        == "cli"
    )


@pytest.mark.parametrize("version", [0, 3, 7, 8])
def test_a_ladder_registry_from_a_development_build_is_not_carried(
    tmp_path, version
) -> None:
    """A ladder step no released line stands at is refused, and the file is the repair.

    The chain begins at the released baselines, so the ladder's *other* steps are
    development builds' — a trunk build at the ladder's last step (8) as much as
    one that stopped at 3, and 0 as much as either, which is a ladder run that was
    killed before it recorded where it got to. None of them is a shape a released
    install has, and a registry a tip install left behind meets the tip wipe
    policy (`docs/releasing.md`): the refusal names the file to delete, and
    nothing was migrated or stamped.

    The schema is built at the baseline and the row is the development build's,
    because the placement reads the **row** and never the shape under it — which
    is why the shape beneath does not enter into the refusal.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _ladder_left_it(db, version)
    _seed_project(db)

    with pytest.raises(RuntimeError, match="unreleased development build") as raised:
        Registry(db)

    assert str(db) in str(raised.value)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        # The refusal comes first: the row still reads what the ladder wrote.
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            version,
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'alembic_version'"
            ).fetchone()
            is None
        )


def test_a_registry_recording_a_newer_revision_fails_loudly(tmp_path) -> None:
    """A revision this build does not carry is refused, not half-read."""
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999'")

    with pytest.raises(
        RuntimeError, match="9999 is not one this build carries.*upgrade clear-record"
    ):
        Registry(db)

    # The refusal ran nothing: the registry still records what it recorded.
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "9999",
        )


@pytest.mark.parametrize("revision", ["0003", "0005"])
def test_a_registry_stamped_at_a_revision_the_cut_dropped_meets_the_wipe_remedy(
    tmp_path, revision
) -> None:
    """A stamp the compression folded away is not told to upgrade the build it is on.

    The chain this release carries is ``0006``–``0009``; a sprint build stamped a
    registry at ``0001``–``0005``, and those revisions are gone. *Upgrade
    clear-record* is the sentence for a registry from a **newer** build and can
    do nothing for someone already on this one, so a revision below the head that
    this build does not carry meets the wipe sentence instead — and the refusal
    comes first, leaving the registry exactly as it was.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _seed_project(db)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (revision,))

    with pytest.raises(RuntimeError, match="unreleased development build") as raised:
        Registry(db)

    assert str(db) in str(raised.value)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (revision,)
        ]


@pytest.mark.parametrize("version", [99, 10])
def test_a_ladder_registry_from_a_newer_version_fails_loudly(tmp_path, version) -> None:
    """The guard the retired ladder enforced survives: a newer version is refused.

    Both numbers are pinned, and **10** is the one that earns its place: 10 is
    the head revision's id, so a ladder number of 10 used to be read as revision
    ``0010`` and the registry was silently stamped at it — the coincidence of
    numbering the ladder path had. 99 names no revision in this chain and refused
    even before the bound existed, which is exactly why it alone could not keep
    the bound honest.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _ladder_left_it(db, version)

    with pytest.raises(
        RuntimeError,
        match=f"{version} is newer than this build carries.*upgrade clear-record",
    ):
        Registry(db)

    # Nothing was applied and nothing was stamped: the refusal comes first.
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            version,
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'alembic_version'"
            ).fetchone()
            is None
        )


def test_a_registry_path_with_a_query_character_opens(tmp_path) -> None:
    """A `?` in the path is a filename character, not the start of a URL query.

    The migration hands SQLAlchemy the URL object it built and never URL text:
    read back out of text, the `?` becomes a query and a *different* file is the
    one that gets migrated.
    """
    db = tmp_path / "what?" / "registry.sqlite3"

    reg = Registry(db)

    assert (
        reg.create_project(
            "Ops",
            actor="console",
        ).slug
        == "ops"
    )
    assert db.exists()
    assert not (tmp_path / "what").exists()  # the file the text form creates


@contextmanager
def _the_run_vocabulary_a_pre_lifecycle_build_declared_in_models() -> Iterator[None]:
    """Lend ``models`` the run vocabulary a build from before ``lifecycle`` used.

    Both helpers below exec a store module read out of the history against
    today's tree, and both hit the same drift: that build asked
    ``clear_record.service.models`` for the run states and origins, which today
    belong to :mod:`clear_record.service.lifecycle` (the states and the moves
    that use them are one declaration). The names are set for the length of the
    exec and taken back afterwards, so the old build still runs as itself — what
    is under test is its schema, not which module a status is declared in.
    """
    lent = {
        name: value
        for name, value in (
            ("RUN_ORIGINS", RUN_ORIGINS),
            ("RUN_STATUSES", RUN_STATUSES),
        )
        if not hasattr(models_module, name)
    }
    for name, value in lent.items():
        setattr(models_module, name, value)
    try:
        yield
    finally:
        for name in lent:
            delattr(models_module, name)


#: The tag the released baseline comes from: the release whose schema revision
#: ``0006`` claims to be. The line's release candidates carry the same schema, so
#: the stable tag is the one that names it.
_RELEASED_TAG = "v0.2.0"


def _released_registry_store() -> types.ModuleType:
    """The released build's own store module, read out of the tag that shipped it.

    ``0006``'s DDL claims to be the schema `v0.2.0` shipped, and nothing else in
    the suite can see the two disagree: every other fixture builds the baseline
    with *this* build's chain, so a drift inside that revision would leave the
    whole suite green. The released build is the only authority on what it
    shipped, so its own module is read out of the history and run — the device
    :func:`_retired_ladder_store` uses for the trunk's ladder — and the registry
    it builds is what the test below opens with this build.

    What a blob out of the history cannot bring with it is its **import list**:
    it is exec'd against today's tree. ``clear_record.service.models`` and
    ``clear_record.core.events`` still answer, but ``clear_record.service.paths``
    does not — the resolver moved to :mod:`clear_record.core.paths` (ADR-0025) —
    so that one module is stood up for the length of the exec and put back
    afterwards. What is under test is the schema a released build wrote; where
    today's tree keeps its path resolver is not what it is about.
    """
    path = "packages/clear-record/src/clear_record/service/store.py"
    blob = subprocess.run(
        ["git", "show", f"{_RELEASED_TAG}:{path}"],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )
    if blob.returncode != 0 or not re.search(
        r"^SCHEMA_VERSION\s*=\s*\d+", blob.stdout, re.MULTILINE
    ):
        # No history to read (an installed tree, a tarball), or a tag that does
        # not carry the ladder at all.
        pytest.skip(
            f"no {_RELEASED_TAG} in this history to read the released store from"
        )
    module = types.ModuleType("released_registry_store")
    lent = "clear_record.service.paths"
    previous = sys.modules.get(lent)
    shim = types.ModuleType(lent)
    shim.registry_path = registry_path  # type: ignore[attr-defined]
    sys.modules[lent] = shim
    try:
        with _the_run_vocabulary_a_pre_lifecycle_build_declared_in_models():
            exec(
                compile(blob.stdout, f"<{_RELEASED_TAG}:{path}>", "exec"),
                module.__dict__,
            )
    finally:
        if previous is None:
            del sys.modules[lent]
        else:
            sys.modules[lent] = previous
    return module


def _schema_shape(db_path: Path) -> dict[str, object]:
    """A registry's schema, as SQLite describes it — not as its DDL reads.

    A `CREATE TABLE` statement is not comparable across builds: the released line
    added two columns by ``ALTER TABLE``, so the text it stored for ``meeting``
    and ``pipeline_run`` differs from a fresh table's while the schemas agree.
    The shape is therefore built out of the pragmas that describe the schema
    itself: every table's columns in declaration order with their types,
    nullability and defaults, every index with its uniqueness, partiality,
    columns and normalized DDL, and every foreign key with its target and
    ``ON DELETE``.
    """

    def normalized(sql: str | None) -> str:
        return " ".join((sql or "").split())

    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        tables = sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT IN ('alembic_version', 'schema_version',"
                " 'sqlite_sequence')"
            )
        )
        columns = {
            name: [
                (row[1], row[2], row[3], row[4], row[5])
                for row in conn.execute(f'PRAGMA table_info("{name}")')
            ]
            for name in tables
        }
        indexes = {
            name: sorted(
                (row[1], row[2], row[3], row[4])
                for row in conn.execute(f'PRAGMA index_list("{name}")')
            )
            for name in tables
        }
        index_columns = {
            name: {
                index[0]: [
                    row[2] for row in conn.execute(f'PRAGMA index_info("{index[0]}")')
                ]
                for index in indexes[name]
            }
            for name in tables
        }
        index_ddl = {
            row[0]: normalized(row[1])
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            )
        }
        foreign_keys = {
            name: sorted(
                (row[3], row[2], row[6])
                for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')
            )
            for name in tables
        }
    return {
        "columns": columns,
        "indexes": indexes,
        "index_columns": index_columns,
        "index_ddl": index_ddl,
        "foreign_keys": foreign_keys,
    }


def test_the_released_builds_own_registry_migrates_to_a_fresh_schema(tmp_path) -> None:
    """The baseline **is** what a released install had, and the tree can see it.

    ``0006`` is only a claim about `v0.2.0` until a released build's own registry
    is put through it, so this builds one the way that build built it — its own
    ladder scripts, its own ``schema_version`` row — and hands it to this build.
    Two things are then checked. The row it kept is still readable, which is the
    upgrading path. And the **shape** of the migrated registry equals a fresh
    one's, table for table, column for column in declaration order, index for
    index, key for key — which is what makes the claim checkable: the released
    tables were created by the released DDL, so a drift in ``0006`` would leave
    them as `v0.2.0` wrote them while a fresh registry took the drifted shape,
    and the two would part company here.
    """
    released = _released_registry_store()
    db = tmp_path / "released.sqlite3"
    released.Registry(str(db))  # the released build's own ladder, at open
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            _BASELINE_LADDER,
        )
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )
    shipped = _schema_shape(db)

    reg = Registry(db)  # this build migrates it, in place

    assert [p.slug for p in reg.list_projects()] == ["ops"]
    fresh = tmp_path / "fresh.sqlite3"
    Registry(fresh)
    assert _schema_shape(db) == _schema_shape(fresh)
    # The deltas really did run: what the released build left is not the head's
    # shape (the run's ownership columns and the one-active-run index are not in
    # it), so the equality above is not two copies of the same untouched schema.
    assert _schema_shape(db) != shipped
    with closing(sqlite3.connect(str(db))) as conn, conn:
        # And the released line's own row was levelled for a `v0.2.x` build.
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            _LADDER_VERSION,
        )


def _retired_ladder_store() -> types.ModuleType:
    """The retired ladder's own store module, from the last commit that carried it.

    The property a registry this build migrated must keep — a build from *before*
    Alembic opens it — is about that build's own code, so no fixture can stand in
    for it: the code has to run. The wheel ships no old build, so the history is
    the source, and the commit is found by **content** rather than by a revision
    id, which a rebase or a squash would invalidate.

    The content searched for is the *assignment* (``SCHEMA_VERSION = 8``), not the
    name: ``git rev-list --all`` walks newest-first, so a mere mention would win —
    and this very file's own history is a mention, since the constant's replacement
    is documented by name where it is declared. A mention is not the ladder.

    What a blob read out of the history cannot bring with it is its **import
    list**: it is exec'd against today's tree, and it asks
    ``clear_record.service.models`` for the run vocabulary — which that build
    declared there, and which today belongs to
    :mod:`clear_record.service.lifecycle` (the states and the moves that use
    them are one declaration). Those two names are lent to ``models`` for the
    length of the exec and taken back afterwards, so the retired build still
    runs as itself. What is under test is the ladder's schema gate; a status's
    declaring module is not what it is about.
    """
    path = "packages/clear-record/src/clear_record/service/store.py"
    listed = subprocess.run(
        ["git", "rev-list", "--all", "--", path],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:  # no history to read (an installed tree, a tarball)
        pytest.skip("no git history to read the retired ladder's store from")
    for commit in listed.stdout.split():
        blob = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=_REPO,
            capture_output=True,
            text=True,
        )
        if blob.returncode == 0 and re.search(
            r"^SCHEMA_VERSION\s*=\s*\d+", blob.stdout, re.MULTILINE
        ):
            module = types.ModuleType("retired_ladder_store")
            with _the_run_vocabulary_a_pre_lifecycle_build_declared_in_models():
                exec(
                    compile(blob.stdout, f"<{commit}:{path}>", "exec"),
                    module.__dict__,
                )
            return module
    pytest.skip("no commit in this history carries the retired ladder")


def test_a_migrated_registry_opens_in_the_retired_ladder(tmp_path) -> None:
    """A build from before Alembic READS AND WRITES the registry this build migrated.

    The ladder's row in ``schema_version`` is the only version state such a build
    reads, and it compares that row with its **own** last version: the row is
    therefore levelled at the ladder's own number (8), never at the head revision's
    id (10 today) — a number above what that build supports is refused outright
    ("registry schema version 10 is newer than this build supports (8)"), which
    makes an upgrade one-way for no reason at all.

    The retired build runs here rather than being described: its own store module
    is read out of the history and its own ``Registry`` opens the file, reads a row
    out of it and writes one back — which is what "a down-level build reads a
    registry this build migrated" has to mean to be worth anything.
    """
    ladder = _retired_ladder_store()
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _ladder_left_it(db, _BASELINE_LADDER)
    _seed_project(db)

    Registry(db)  # this build migrates it, and levels the ladder's row

    reg = ladder.Registry(str(db))  # the retired build runs its own gate
    assert [(project.slug, project.name) for project in reg.list_projects()] == [
        ("ops", "Ops")
    ]
    assert reg.get_project("ops").notes == ""
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
    )  # and writes with its own DDL
    assert reg.get_meeting("ops", "kickoff").id == meeting.id
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            ladder.SCHEMA_VERSION,
        )
    assert ladder.SCHEMA_VERSION == _LADDER_VERSION
    # And the head really is above the ladder, so the levelling is doing work.
    head = ScriptDirectory.from_config(_alembic_config(db)).get_current_head()
    assert head is not None and int(head) > _LADDER_VERSION

    # The other side of the same boundary: a registry *this* build creates
    # carries no such record at all, so a build from before Alembic runs its
    # ladder there and stops at the first step it cannot re-run (the notes
    # column) — which is why the row is written for the registries the ladder
    # itself wrote, and only for those.
    fresh = tmp_path / "fresh.sqlite3"
    Registry(fresh)
    assert _version_tables(fresh) == ["alembic_version"]


# --- the mapping the registry reads and writes through (ADR-0030) ----------- #


def test_the_mapping_describes_every_column_uniqueness_and_foreign_key_the_revisions_own(
    tmp_path,
) -> None:
    """Alembic owns the schema; the entities only describe it (ADR-0030).

    So the two can drift, and a drift is silent until a query fails or a value
    comes back wrong: a revision that adds a column the mapping does not carry, a
    mapped column no table has, a mapped type the table does not hold, or a
    uniqueness the DDL declares and the mapping has forgotten — which is the one
    that turns a duplicate into an ``IntegrityError`` the store reads as "already
    exists". ``--autogenerate`` cannot catch any of it while ``target_metadata``
    stays ``None`` by decision (``migrations/env.py``), so this is the net: every
    table, every column with its type and nullability, every declared uniqueness
    and every declared foreign key, against the head revision's own schema.

    The uniqueness is read on both sides, and both directions are asserted — a
    rule declared in one place and not the other fails here whether the missing
    half is the mapping's or the revision's. It is in two halves because the
    schema states it in two: a table's ``UNIQUE`` constraints (which SQLite
    reports as origin ``u``) and the separately created indexes a later revision
    adds (origin ``c``), of which revision 0009's partial unique index over the
    active run of a meeting is one — its predicate compared with the DDL's, so a
    mapping and a revision that disagree about which states are *active* cannot
    both keep this green.
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)
    mapped_columns = {
        name: {
            column.name: (str(column.type), column.nullable) for column in table.columns
        }
        for name, table in entities.Base.metadata.tables.items()
    }
    mapped_uniques = {
        name: {
            frozenset(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        for name, table in entities.Base.metadata.tables.items()
    }
    # Every *unique index* the mapping declares, as (columns, partial predicate):
    # the predicate is the index's dialect-specific ``WHERE``, or ``None`` for an
    # index over the whole table.
    mapped_unique_indexes = {
        name: {
            index.name: (
                tuple(column.name for column in index.columns),
                _normalized(
                    str(index.dialect_options["sqlite"]["where"])
                    if index.dialect_options["sqlite"].get("where") is not None
                    else None
                ),
            )
            for index in table.indexes
            if index.unique
        }
        for name, table in entities.Base.metadata.tables.items()
    }
    # Every foreign key the mapping declares, as (column, target table, ondelete).
    # The schema mixes ``CASCADE`` with ``SET NULL`` (``artifact.run_id``), so an
    # ``ON DELETE`` drift is a rule that changes what a delete does — silently,
    # until something is deleted.
    mapped_foreign_keys = {
        name: {
            (fk.parent.name, fk.column.table.name, fk.ondelete)
            for fk in table.foreign_keys
        }
        for name, table in entities.Base.metadata.tables.items()
    }
    with closing(sqlite3.connect(str(db))) as conn, conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT IN ('alembic_version', 'sqlite_sequence')"
            )
        ]
        # PRAGMA table_info is (cid, name, type, notnull, dflt_value, pk). An
        # ``INTEGER PRIMARY KEY`` is the rowid and is not reported NOT NULL, but
        # it can never be null — which is how the mapping declares it.
        actual_columns = {
            name: {
                column[1]: (column[2].upper(), column[3] == 0 and column[5] == 0)
                for column in conn.execute(f'PRAGMA table_info("{name}")')
            }
            for name in names
        }
        # The DDL of every index the schema carries, by name: what SQLite stores
        # is the ``CREATE INDEX`` statement the revision ran, predicate included.
        index_ddl = {
            name: sql
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            )
        }
        # PRAGMA index_list is (seq, name, unique, origin, partial): origin ``u``
        # is a UNIQUE constraint of the table's own DDL (``pk`` is the rowid's,
        # and ``c`` an index a revision created separately).
        actual_uniques = {
            name: {
                frozenset(
                    column[2]
                    for column in conn.execute(f'PRAGMA index_info("{index[1]}")')
                )
                for index in conn.execute(f'PRAGMA index_list("{name}")')
                if index[2] == 1 and index[3] == "u"
            }
            for name in names
        }
        actual_unique_indexes = {
            name: {
                index[1]: (
                    tuple(
                        column[2]
                        for column in conn.execute(f'PRAGMA index_info("{index[1]}")')
                    ),
                    _index_predicate(index_ddl.get(index[1])),
                )
                for index in conn.execute(f'PRAGMA index_list("{name}")')
                if index[2] == 1 and index[3] == "c"
            }
            for name in names
        }
        # PRAGMA foreign_key_list is (id, seq, table, from, to, on_update,
        # on_delete, match). SQLite reports "NO ACTION" for the default, which is
        # what the mapping spells as no ``ondelete`` at all.
        actual_foreign_keys = {
            name: {
                (row[3], row[2], None if row[6] == "NO ACTION" else row[6])
                for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')
            }
            for name in names
        }

    assert set(mapped_columns) == set(actual_columns)
    for name in sorted(actual_columns):
        mapped, stored = mapped_columns[name], actual_columns[name]
        assert mapped == stored, f"{name} columns: " + ", ".join(
            f"{key}: mapped={mapped.get(key)!r} actual={stored.get(key)!r}"
            for key in sorted(set(mapped) | set(stored))
            if mapped.get(key) != stored.get(key)
        )
        assert mapped_uniques[name] == actual_uniques[name], (
            f"{name} uniques: mapped={mapped_uniques[name]!r}"
            f" actual={actual_uniques[name]!r}"
        )
        assert mapped_unique_indexes[name] == actual_unique_indexes[name], (
            f"{name} unique indexes: mapped={mapped_unique_indexes[name]!r}"
            f" actual={actual_unique_indexes[name]!r}"
        )
        assert mapped_foreign_keys[name] == actual_foreign_keys[name], (
            f"{name} foreign keys: mapped={mapped_foreign_keys[name]!r}"
            f" actual={actual_foreign_keys[name]!r}"
        )


def test_the_active_run_index_arrives_with_revision_0009(tmp_path) -> None:
    """The database's one-active-run rule is a revision, and it refuses a row.

    A registry at the released baseline — every release before this one —
    carries no such index, and opening it, which is what migrates it, is what
    gives it one: the same index a registry created fresh carries, because both
    are the DDL revision 0009 ran. The rule is then exercised the way a second
    *writer* exercises it: one connection, one bare INSERT, no service call in
    the way.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    assert _index_sql(db, "pipeline_run_active_meeting") is None

    reg = Registry(db)  # migrates the baseline's deltas, this index among them
    fresh = tmp_path / "fresh.sqlite3"
    Registry(fresh)
    assert (
        _index_sql(db, "pipeline_run_active_meeting")
        == _index_sql(fresh, "pipeline_run_active_meeting")
        == "CREATE UNIQUE INDEX pipeline_run_active_meeting ON pipeline_run"
        " (meeting_id) WHERE status IN ('queued', 'running')"
    )

    # Both active states are refused for a meeting that has one, and a finished
    # run is not refused at all: the index is partial, and the states it names
    # are the states the guard reads.
    reg.create_project(
        "Ops",
        actor="console",
    )
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )
    active = reg.create_run(
        meeting.id,
        origin="console",
        actor="console",
    )
    with closing(sqlite3.connect(str(db))) as conn, conn:
        for status in ("queued", "running"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pipeline_run (meeting_id, status, created_at)"
                    " VALUES (?, ?, 'now')",
                    (meeting.id, status),
                )
        conn.execute(
            "INSERT INTO pipeline_run (meeting_id, status, created_at)"
            " VALUES (?, 'done', 'now')",
            (meeting.id,),
        )

    assert [(run.id, run.status) for run in reg.list_runs(meeting.id)] == [
        (active.id + 1, "done"),
        (active.id, "queued"),
    ]


def test_a_registry_that_already_holds_two_active_runs_opens(tmp_path) -> None:
    """The state the rule forbids is one the old code produced, and it still opens.

    Two submissions racing past the guard wrote two active runs for one meeting,
    and a unique index cannot be created over those rows — a registry like that
    would refuse to open, on every start, forever. So revision 0009 reconciles
    before it creates the index, and the keeper is the run the service would still
    call live: a ``running`` row **that names an owner** is kept (SQL cannot probe
    the owner's process, and anything weaker would risk interrupting a run that is
    really executing and admitting a second pipeline beside it), and among the
    rest the meeting's **newest** run wins — the submission the user just made,
    not the stale one they resubmitted because it looked stuck. The rest end as
    ``interrupted``, with a reason that names the run that survived, and a meeting
    that holds one active run — or none — is left exactly as it was.
    The fixture is the chain one revision short of the rule rather than a
    released baseline, and that is the point: the rows that break the rule can
    name an owner only where the run-ownership columns exist and the rule does
    not (``0008``), which is the one shape that makes the keeper's **first**
    branch — a ``running`` row that names an owner — expressible at all. A
    released registry's racing rows all predate the ``owner`` column, so that
    shape is this same statement with every owner ``NULL``, and the
    two-queued-runs half below is exactly it.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0008")
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )
        for slug in ("kickoff", "standup", "retro", "rehearsal"):
            conn.execute(
                "INSERT INTO meeting (project_id, slug, title, status, created_at)"
                " VALUES (1, ?, ?, 'running', 'now')",
                (slug, slug),
            )
        conn.executemany(
            "INSERT INTO pipeline_run"
            " (meeting_id, status, created_at, started_at, owner)"
            " VALUES (?, ?, 'now', ?, ?)",
            (
                # What the race left: two queued runs for one meeting...
                (1, "queued", None, None),
                (1, "queued", None, None),
                # ... and a running run that names an owner, with a queued one
                # behind it.
                (2, "queued", None, None),
                (2, "running", "now", "host:1"),
                # What the rule wants: one active run, and a run that had ended.
                (3, "queued", None, None),
                (3, "done", "now", None),
                # A running run nothing can be executing — the shape every row the
                # retired ladder wrote has, no owner at all — and the submission
                # the user made after it: the queued one survives.
                (4, "running", "now", None),
                (4, "queued", None, None),
            ),
        )

    reg = Registry(db)

    # The newest of the two queued runs is the meeting's; the older one is ended,
    # and its reason names the run that survived.
    older, newer = sorted(reg.list_runs(1), key=lambda run: run.id)
    assert (older.id, older.status) == (1, "interrupted")
    assert older.ended_at is not None
    assert "run 2" in (older.error or "")
    assert (newer.id, newer.status, newer.ended_at, newer.error) == (
        2,
        "queued",
        None,
        None,
    )

    # A running row that names an owner is the meeting's one, whatever a queued
    # row behind it says.
    waiting, running = sorted(reg.list_runs(2), key=lambda run: run.id)
    assert (waiting.id, waiting.status) == (3, "interrupted")
    assert "run 4" in (waiting.error or "")
    assert (running.id, running.status, running.ended_at) == (4, "running", None)

    # And a meeting with nothing to reconcile is untouched.
    alone, ended = sorted(reg.list_runs(3), key=lambda run: run.id)
    assert (alone.id, alone.status, alone.ended_at, alone.error) == (
        5,
        "queued",
        None,
        None,
    )
    assert (ended.id, ended.status) == (6, "done")

    # A running row with no owner is one nothing can be executing: the user's
    # later submission is kept instead of it.
    dead, submitted = sorted(reg.list_runs(4), key=lambda run: run.id)
    assert (dead.id, dead.status) == (7, "interrupted")
    assert "run 8" in (dead.error or "")
    assert (submitted.id, submitted.status) == (8, "queued")

    assert _index_sql(db, "pipeline_run_active_meeting") is not None
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0012",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO pipeline_run (meeting_id, status, created_at)"
                " VALUES (?, 'queued', 'now')",
                (alone.meeting_id,),
            )


def test_the_claim_and_the_compare_decide_in_one_statement(tmp_path) -> None:
    """The two atomic transitions stay one statement each (ADR-0030).

    The claim and the reconciliation are kept as Core expressions because their
    guarantee *is* the statement: the write lock decides the claim inside it, and
    the compare travels to the database with it. A mapping that read the row,
    decided in Python and wrote it back would keep this file's behaviour tests
    green — nothing here contends — and lose that guarantee, so what is asserted
    is the shape: the transition is the call's **first** statement (there is no
    read before it to decide from), and the values the decision rests on are that
    statement's own parameters.
    """
    reg = _registry(tmp_path)
    reg.create_project(
        "Ops",
        actor="console",
    )
    meeting = reg.create_meeting(
        "ops",
        "Kickoff",
        actor="console",
    )
    run = reg.create_run(
        meeting.id,
        actor="console",
    )

    seen: list[tuple[str, tuple]] = []

    @event.listens_for(reg._engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, parameters, _context, _many) -> None:
        seen.append((" ".join(statement.split()), parameters))

    claimed = reg.claim_run(
        run.id,
        owner="host:1",
        actor="console",
    )
    assert claimed is not None and claimed.status == "running"
    # The transition, the row it wrote read back, and the audit row the call
    # appends for itself (ADR-0033) — which is a statement of its own, after the
    # claim's unit of work has closed.
    assert len(seen) == 3, seen
    statement, parameters = seen[0]
    assert statement.startswith("UPDATE pipeline_run")
    assert "EXISTS" in statement.upper()  # the node's rule, inside the WHERE
    assert "host:1" in parameters and run.id in parameters

    seen.clear()
    reaped = reg.interrupt_run(
        run.id,
        observed=claimed,
        ended_at="now",
        error="gone",
        progress={},
        actor="console",
    )
    assert reaped is not None and reaped.status == "interrupted"
    assert len(seen) == 3, seen  # the compare-and-set, the read back, the audit row
    statement, parameters = seen[0]
    assert statement.startswith("UPDATE pipeline_run")
    assert claimed.owner in parameters and claimed.heartbeat_at in parameters

    # The behaviour the shape buys: the same snapshot again is a stale one, and
    # a stale observation does not interrupt a row it no longer describes.
    assert (
        reg.interrupt_run(
            run.id,
            observed=claimed,
            ended_at="later",
            error="stale",
            progress={},
            actor="console",
        )
        is None
    )


def test_a_version_table_holding_two_revisions_opens_and_is_reduced(tmp_path) -> None:
    """Two rows in ``alembic_version`` read as a multi-head state, and were fatal.

    This is what two surfaces opening one registry at once left behind while
    nothing serialized the migration: ``Requested revision 0009 overlaps with
    other requested revisions 0008`` on every later open — the user's rows still
    in the file, and the console, the MCP server and the tray unable to start.
    Opening it reduces the table to the head-most revision, which is the one the
    schema actually carries: the rows recorded steps that ran, the chain is
    linear, and the schema holds every step up to the newest of them.
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)
    _seed_project(db)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('0008')")

    reg = Registry(db)

    assert [project.slug for project in reg.list_projects()] == ["ops"]
    assert (
        reg.create_meeting(
            "ops",
            "Kickoff",
            actor="console",
        ).slug
        == "kickoff"
    )
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0012",)
        ]


def test_an_empty_version_table_on_a_built_schema_opens(tmp_path) -> None:
    """A stamp that never landed is a misplaced stamp, not "no registry here yet".

    An open killed between Alembic's creation of ``alembic_version`` and its final
    stamp leaves that table present and empty over a schema built to *some* point.
    Reading it as a registry with no history replays every revision from the base:
    the baseline only creates (each table ``IF NOT EXISTS``), each delta that adds
    a column is guarded on that column (``0007``, ``0008``), and revision 0009
    leaves the index it finds when that index is its own — so the replay
    converges and the registry is current again, which is what makes the repair
    safe to rely on. (Replaying the *retired ladder's* steps was not: a second run
    died on ``duplicate column name: notes``, and every later open died with it.
    That is history now — the ladder's steps are not this chain's revisions — and
    the guard that the replay needs is the one above.)
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)
    _seed_project(db)
    index_before = _index_sql(db, "pipeline_run_active_meeting")
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("DELETE FROM alembic_version")

    reg = Registry(db)

    assert [project.slug for project in reg.list_projects()] == ["ops"]
    assert (
        reg.create_meeting(
            "ops",
            "Kickoff",
            actor="console",
        ).slug
        == "kickoff"
    )
    assert _index_sql(db, "pipeline_run_active_meeting") == index_before
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0012",)
        ]


#: The busy-timeout budget the short-timeout engine boxes carry: small enough
#: that a test can wait out several of them in the time the registry's own
#: five-second policy would take to expire once.
_BOX_TIMEOUT = 0.2

#: The per-connection policy as the *file* reports it, which is what a durable
#: assertion about the contract can pin: milliseconds of waiting for a writer,
#: SQLite's own code for FULL, and the two guards the audit record leans on. It
#: is here rather than read off ``store`` because a test that imported the values
#: it compares against would pass whatever the code said.
_CONNECTION_POLICY = {
    "busy_timeout": 5000,
    "synchronous": 2,
    "foreign_keys": 1,
    "recursive_triggers": 1,
}


def _engine_timing_out_box(path: Path, timeout: float = _BOX_TIMEOUT):
    """An engine like the registry's own, with a busy timeout a test can wait out.

    It is not the registry's policy: the budget here is the *test's*, set through
    the driver's own ``timeout`` connect argument (which is the knob the policy's
    ``PRAGMA busy_timeout`` sets on the registry's connections), so the
    short-timeout tests do not set that pragma and do not spend five seconds
    waiting out a decision they are asserting.
    """
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        poolclass=NullPool,
        connect_args={"timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    return engine


def test_a_migration_another_surface_holds_is_refused_with_a_message(
    tmp_path, monkeypatch
) -> None:
    """The transient state is named, and says what to do, where a traceback was.

    The registry migrates when it opens (ADR-0030), so one surface can meet
    another's migration: the step takes SQLite's write lock for the whole of it
    and waits for that lock up to the driver's timeout. What comes out when the
    lock is still held is :class:`RegistryLocked` — the file named, nothing
    changed, ``retry`` — instead of the raw driver error, which arrived as an
    ``OperationalError`` traceback from a start-up path that has no user to read
    it.
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)
    monkeypatch.setattr(
        store_module, "_engine", lambda path: _engine_timing_out_box(path)
    )
    holder = sqlite3.connect(str(db))
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE project SET name = 'busy'")
        with pytest.raises(RegistryLocked) as raised:
            Registry(db)
    finally:
        holder.rollback()
        holder.close()

    assert str(db) in str(raised.value)
    assert "retry" in str(raised.value)


#: Look-alike objects an index of *this* name may carry. None is revision 0009's
#: own, and each must refuse the registry: the first because the shape is not the
#: rule at all, the second because it is the rule **narrowed** to one state, the
#: third because the predicate is right and the column is not.
_LOOK_ALIKES = (
    "CREATE INDEX pipeline_run_active_meeting ON pipeline_run (meeting_id)",
    "CREATE UNIQUE INDEX pipeline_run_active_meeting ON pipeline_run (meeting_id)"
    " WHERE status = 'running'",
    "CREATE UNIQUE INDEX pipeline_run_active_meeting ON pipeline_run (id)"
    " WHERE status IN ('queued', 'running')",
)


@pytest.mark.parametrize("planted", _LOOK_ALIKES)
def test_an_index_of_this_name_that_is_not_the_declared_one_is_refused(
    tmp_path, planted
) -> None:
    """A look-alike object must not make the one-active-run rule optional.

    ``CREATE UNIQUE INDEX IF NOT EXISTS`` accepted whatever already carried the
    name — so a plain index someone had created by hand left the rule unenforced
    while the mapping and the parity test still declared it, and the revision's
    whole point was lost in silence. Revision 0009 compares what it finds instead:
    its own index is left alone (a re-run converges on it, and a successor's
    *widening* of the state list is still its own — see the test below), and
    anything else refuses the registry with the object's own DDL in the message,
    the reconciliation the revision had already run rolled back with it.

    The shapes here are the ones a loose comparison would wave through: sharing
    the name and the column is not enough, and neither is sharing the shape — the
    rule is the predicate, so a predicate that is not this rule with at least
    these states is a different rule.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, _BASELINE)
    _seed_project(db)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(planted)

    with pytest.raises(RuntimeError) as raised:
        Registry(db)

    assert "pipeline_run_active_meeting" in str(raised.value)
    assert _index_sql(db, "pipeline_run_active_meeting") == _normalized(planted)
    with closing(sqlite3.connect(str(db))) as conn, conn:
        # Nothing was half-applied: the refused open left the registry at the
        # baseline the deltas were running from.
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (_BASELINE,)
        ]


def test_a_successors_widened_index_is_left_as_this_revisions_own(tmp_path) -> None:
    """A *wider* state list is still revision 0009's index, and the repair opens.

    Adding an active state is one edit to ``ACTIVE_RUN_STATUSES`` plus a revision
    that restates this index with the new state in it (the rule the sprint's
    trapdoor finding settled), and a successor therefore leaves an index whose
    state list is longer than this revision's DDL. The replay-from-base repair
    runs 0009 again over that schema, so the comparison has to recognise it: the
    shape is this revision's own and the predicate is this rule with more states,
    which is stricter about the states it names and never narrower. Comparing DDL
    text refused exactly this registry — the repair could not recover one whose
    first pass was killed.
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)
    widened = _normalized(
        "CREATE UNIQUE INDEX pipeline_run_active_meeting ON pipeline_run"
        " (meeting_id) WHERE status IN ('queued', 'running', 'paused')"
    )
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("DROP INDEX pipeline_run_active_meeting")
        conn.execute(widened)
        conn.execute("DELETE FROM alembic_version")

    Registry(db)  # the replay from the base must not refuse it

    assert _index_sql(db, "pipeline_run_active_meeting") == widened
    with closing(sqlite3.connect(str(db))) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0012",)
        ]


def test_the_database_rule_names_the_active_statuses_the_service_declares(
    tmp_path,
) -> None:
    """One declaration of what "active" means, and the database's copy of it.

    The guard the run manager asks, the console's run views and the mapping read
    the lifecycle's :data:`~clear_record.service.lifecycle.ACTIVE_RUN_STATUSES` —
    the mapping through
    :func:`~clear_record.service.lifecycle.active_run_predicate` — while revision
    0009 states its own ``WHERE``, because a revision states its own DDL and cannot
    import today's application. This reads the *built* index's predicate back and
    compares it with the declaration's own rendering. The narrower readers — the
    claim (``CLAIM.sources``) and the reconciliation's compare-and-set
    (``INTERRUPT.sources``) — are exercised by the tests above, which cannot pass
    if either drifts.
    """
    db = tmp_path / "registry.sqlite3"
    Registry(db)

    assert (
        _index_predicate(_index_sql(db, "pipeline_run_active_meeting"))
        == active_run_predicate()
    )


def test_a_revision_written_the_documented_way_is_numbered_like_the_chain(
    tmp_path,
) -> None:
    """``alembic revision -m`` names a new revision in the chain's own numbering.

    ``script.py.mako`` writes ``${up_revision}``, and Alembic's default for it is a
    hex uuid — which is why the ladder's number is a fixed constant
    (``store._LADDER_VERSION``) and never read off the head id. The ids stay
    decimal because ``alembic.ini`` runs ``env.py`` for ``revision`` too, and
    ``env.py`` names the file after the newest revision: the documented workflow
    cannot produce a hex id without that configuration changing.
    """
    script_location = tmp_path / "migrations"
    shutil.copytree(_MIGRATIONS, script_location)
    ini = tmp_path / "alembic.ini"
    ini.write_text(
        f"[alembic]\nscript_location = {script_location}\n"
        "revision_environment = true\n",
        encoding="utf-8",
    )

    made = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ini),
            "revision",
            "-m",
            "a next change",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )

    assert made.returncode == 0, made.stderr
    written = sorted(script_location.glob("versions/0013_a_next_change.py"))
    assert [path.name for path in written] == ["0013_a_next_change.py"]
    assert 'revision: str = "0013"' in written[0].read_text(encoding="utf-8")


def test_the_alembic_ini_names_the_history_this_build_opens() -> None:
    """The developer CLI reads the same history the application does.

    ``store._SCRIPT_LOCATION`` is what an installed wheel resolves (a package
    resource) and ``alembic.ini`` carries the same value for the developer CLI.
    Nothing read the ini, so a rename in one place would have pointed the other at
    a directory with no revisions while the application kept working — the drift
    only a human notices. The ini's ``revision_environment`` is part of the same
    contract: it is what makes ``alembic revision`` run ``env.py``, where the ids
    are pinned to the chain's numbering.
    """
    ini = (_REPO / "alembic.ini").read_text(encoding="utf-8")

    assert f"script_location = {store_module._SCRIPT_LOCATION}" in ini
    assert "revision_environment = true" in ini


def test_the_alembic_ini_parses_and_the_documented_command_loads(tmp_path) -> None:
    """The developer CLI's own file is *readable*, and the documented use of it runs.

    The test above reads this file as text and greps two substrings, which is
    exactly what let a broken line through: a comment's continuation lost its
    ``#``, so ``configparser`` refused the file before Alembic could reach
    ``env.py``. Every documented CLI use died there — ``alembic -x db=… upgrade
    head`` and the ``alembic revision`` flow — and the suite stayed green, because
    the application never reads this file (``store._alembic_config`` builds its
    ``Config`` in code, ADR-0030) and the tests that drive ``env.py`` write an ini
    of their own.

    So the two things a text assertion cannot see are pinned here: the file
    **parses**, and the documented command's **environment loads** over it — the
    chain builds a temp registry from nothing, which is the door a broken line
    closes.
    """
    config = configparser.ConfigParser()
    config.read(_REPO / "alembic.ini")  # ParsingError is what a broken line raises
    assert config["alembic"]["script_location"] == store_module._SCRIPT_LOCATION

    db = tmp_path / "registry.sqlite3"
    upgraded = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(_REPO / "alembic.ini"),
            "-x",
            f"db={db}",
            "upgrade",
            "head",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )

    assert upgraded.returncode == 0, upgraded.stderr
    # The schema the CLI just built is one this application opens, with no
    # migration of its own to run.
    assert Registry(db).list_projects() == []


def test_the_migration_runs_under_the_registrys_foreign_key_rule(tmp_path) -> None:
    """The schema's history is built under the same FK rule as the app's writes.

    SQLite sets ``PRAGMA foreign_keys`` per connection and defaults it **off**, so
    the registry's engine turns it on for every connection it makes. The migration
    used to run on a connection of its own, with the default: invisible until a
    revision rebuilds a table (the create-copy-drop-rename pattern), where the
    copy's constraints are whatever the DDL says under a rule nothing had set.

    The chain is driven here the way the developer CLI drives it, over a copy with
    one added revision that records the pragma it ran under.
    """
    script_location = tmp_path / "migrations"
    shutil.copytree(_MIGRATIONS, script_location)
    (script_location / "versions" / "0013_pragma_probe.py").write_text(
        '"""A probe revision: record the FK pragma this migration runs under."""\n'
        "\n"
        "from alembic import op\n"
        "\n"
        'revision: str = "0013"\n'
        'down_revision: str | None = "0012"\n'
        "branch_labels = None\n"
        "depends_on = None\n"
        "\n"
        "\n"
        "def upgrade() -> None:\n"
        '    value = op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar()\n'
        '    op.execute(f"CREATE TABLE pragma_probe AS SELECT {int(value)}'
        ' AS foreign_keys")\n'
        "\n"
        "\n"
        "def downgrade() -> None:\n"
        '    raise NotImplementedError("forward-only")\n',
        encoding="utf-8",
    )
    ini = tmp_path / "alembic.ini"
    ini.write_text(
        f"[alembic]\nscript_location = {script_location}\n", encoding="utf-8"
    )
    db = tmp_path / "registry.sqlite3"

    migrated = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ini),
            "-x",
            f"db={db}",
            "upgrade",
            "head",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )

    assert migrated.returncode == 0, migrated.stderr
    with closing(sqlite3.connect(str(db))) as conn, conn:
        assert conn.execute("SELECT foreign_keys FROM pragma_probe").fetchone() == (1,)


# --- the connection's policy, and the journal the file reads through -------- #
def test_a_read_is_not_refused_while_another_surface_holds_the_write_lock(
    tmp_path,
) -> None:
    """A read does not wait behind a writer's lock — the node's own shape.

    The node reads while it writes: a run appends a report per chunk, the
    heartbeat refreshes its row, and each of those mutations appends the audit
    row ADR-0033 requires, while the console's status page, the CLI's poll and a
    test's ``list_run_events`` read the same registry. In SQLite's default
    rollback journal a commit holds ``PENDING`` and then ``EXCLUSIVE`` while it
    writes the journal, syncs the database and deletes the journal again, and
    both locks refuse every other connection's read lock for the whole of it. A
    read issued inside one of those windows therefore waits out the driver's
    busy timeout (5 s) and then fails with ``database is locked`` — the flake a
    cancel test's poll met under the gate's load, where the node's writes kept
    the windows tiled. The write-ahead log the registry's file is opened on
    (:func:`clear_record.service.store._write_ahead_log`) is what removes that
    class: readers take no lock a writer holds, so the read below answers while
    the writer is still holding the lock.

    The writer's state is *held* here rather than raced for: ``BEGIN EXCLUSIVE``
    is the lock a commit holds across the journal write and the syncs, kept open
    deliberately, so a refusal is a verdict and not a timing.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops", actor="console")
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(tmp_path),
        actor="console",
    )
    run = registry.create_run(meeting.id, origin="console", actor="console")
    registry.add_run_event(run.id, JobEvent(stage="ingest"), actor="queue")

    holder = sqlite3.connect(str(registry.db_path), timeout=5.0)
    holder.execute("BEGIN EXCLUSIVE")
    # A write that changes nothing, to hold the lock: the version table carries
    # one row by construction, so this statement is the lock and nothing else.
    holder.execute("UPDATE alembic_version SET version_num = version_num")
    read: list[JobEvent] = []
    failure: list[BaseException] = []

    def read_while_the_writer_holds_the_lock() -> None:
        try:
            read.extend(registry.list_run_events(run.id))
        except BaseException as exc:  # the refusal this test is about
            failure.append(exc)

    try:
        reader = threading.Thread(
            target=read_while_the_writer_holds_the_lock, daemon=True
        )
        reader.start()
        reader.join(1.0)
        assert not reader.is_alive(), (
            "a read waited behind the writer's lock: this registry's journal "
            "refuses readers while a commit holds the database"
        )
        assert not failure, failure[0]
        assert [event.stage for event in read] == ["ingest"]
    finally:
        holder.rollback()
        holder.close()


def _journal_mode(db_path: Path) -> str:
    """The file's journal mode, read back from its header."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _registry_log_events(event: str) -> list[dict]:
    """Every record of that name the node's log holds, oldest first.

    Read off the sink itself — the test environment redirects the log directory
    into ``tmp_path`` — so what is asserted is what a reader of the log meets,
    not what the code meant to write.
    """
    try:
        lines = diagnostics.log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [
        record
        for record in (json.loads(line) for line in lines if line.strip())
        if record.get("event") == event
    ]


def _release_lock(holder: sqlite3.Connection) -> None:
    """Give a held lock back; a connection already released is not an error.

    A timer may release the connection before the test's own ``finally`` does
    (the waiting test does exactly that), and closing it twice raises.
    """
    try:
        holder.rollback()
        holder.close()
    except sqlite3.ProgrammingError:  # a second release
        pass


def _in_the_rollback_journal(db_path: Path) -> None:
    """Put a registry on disk back in the rollback journal a released line left.

    This build creates registries on the log, so a fixture only reaches the
    conversion by taking one back: the mode is a property of the file, and this
    is the file a candidate meets on an upgrade.
    """
    with closing(sqlite3.connect(str(db_path))) as conn:
        mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    assert str(mode).lower() == "delete"


def test_an_existing_registry_converts_to_the_write_ahead_log_on_open(
    tmp_path,
) -> None:
    """A registry already on disk gains the log, once, and keeps its rows.

    The mode belongs to the **file**, so one open does the conversion and every
    open after it reads the answer back — which is what extends the property
    ``test_a_read_is_not_refused_while_another_surface_holds_the_write_lock``
    leans on to the registries that already exist, not only to the ones this
    build created. The log's own files are the other half of it: they are beside
    the registry while a connection holds one, and the last close checkpoints
    them back into the database and takes them away.

    What opened the file and what the file *is* are one fact: the mode the
    registry recorded is the mode the pragma answered with, and the open said
    nothing about a degradation. That starts at the **first** open — a registry
    this build creates is on the log before it can hold a row — and the fixture
    below takes the mode back only to meet it again as an upgrade would.
    """
    db_path = tmp_path / "registry.sqlite3"
    first = _registry(tmp_path)
    assert first.journal_mode == "wal" == _journal_mode(db_path)
    first.create_project("Ops", actor="console")
    _in_the_rollback_journal(db_path)
    assert _journal_mode(db_path) == "delete"

    # The same file, opened again: the row it held is still there, and the file
    # now carries the log.
    reopened = Registry.open(db_path=db_path)
    assert [project.slug for project in reopened.list_projects()] == ["ops"]
    assert _journal_mode(db_path) == "wal"
    assert reopened.journal_mode == "wal" == _journal_mode(db_path)
    assert not _registry_log_events(store_module.JOURNAL_MODE_DEGRADED)

    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    assert not wal.exists() and not shm.exists()  # the pool holds nothing open
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE project SET slug = slug")
        assert wal.exists() and shm.exists()
        writer.commit()
    finally:
        writer.close()
    assert not wal.exists() and not shm.exists()


def test_an_open_whose_conversion_is_refused_serves_in_the_mode_it_kept(
    tmp_path, monkeypatch
) -> None:
    """A refused conversion is tolerated, recorded, stated — and over at the next open.

    The lock is **another writer's reservation** (``BEGIN IMMEDIATE``), and the
    pragma is refused **at once** — SQLite does not make a mode change wait for a
    lock it cannot take while that reservation stands (measured: 0.00 s for this
    shape; a *reader's* shared lock is the shape that waits, and
    ``test_a_conversion_that_must_wait_a_reader_pays_the_whole_budget`` drives
    that one). The file keeps the rollback journal, and the registry then
    **serves** — this one migrates and reads its rows — which is the tolerance
    that stays; what is new is that it is no longer silent: the mode is recorded
    on the registry (:attr:`Registry.journal_mode`), stated in the node's log
    (:data:`store.JOURNAL_MODE_DEGRADED`, with the mode and the reason) and, from
    there, carried by the diagnostics bundle.

    The lock is given back **at the refusal** rather than on a timer, so what
    this test fixes is the order and not a clock: the reservation is held while
    the pragma is refused, and gone before the migration's own write lock is
    attempted. A lock that *stayed* held would refuse the migration too — and in
    the rollback journal any surviving lock forbids its COMMIT as well (measured:
    a read-only ``BEGIN IMMEDIATE`` transaction cannot commit while another
    connection holds ``SHARED``) — which is why a registry serving in the
    rollback journal is only reachable along this transient path, and why the
    mode is worth stating when it happens instead of being left for whoever next
    debugs a reader that was refused.
    """
    db_path = tmp_path / "registry.sqlite3"
    _registry(tmp_path).create_project("Ops", actor="console")
    _in_the_rollback_journal(db_path)
    assert _journal_mode(db_path) == "delete"
    monkeypatch.setattr(store_module, "_engine", _engine_timing_out_box)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")

    real_conversion = store_module._write_ahead_log
    attempts: list[tuple[str, str | None]] = []

    def the_other_surface_gives_the_lock_back_at_the_refusal(engine):
        result = real_conversion(engine)
        if not attempts:
            # The conversion is over — refused at once by the reservation — so
            # the lock has done the work this test is about, and it is released
            # before the migration asks for it.
            _release_lock(holder)
        attempts.append(result)
        return result

    monkeypatch.setattr(
        store_module,
        "_write_ahead_log",
        the_other_surface_gives_the_lock_back_at_the_refusal,
    )
    try:
        registry = Registry.open(db_path=db_path)

        assert attempts[0][0] == "delete"  # asked for the log, did not get it
        assert registry.journal_mode == "delete"  # ... and records what it has
        assert _journal_mode(db_path) == "delete"
        assert [project.slug for project in registry.list_projects()] == ["ops"]
    finally:
        _release_lock(holder)

    degraded = _registry_log_events(store_module.JOURNAL_MODE_DEGRADED)
    assert [(record["level"], record["mode"]) for record in degraded] == [
        ("warning", "delete")
    ]
    # The reason is the registry's own refusal, not a sentence of its own: a
    # reader of the log must be able to tell a lock apart from a filesystem that
    # will not host the log at all.
    assert "lock" in degraded[0]["reason"], degraded[0]["reason"]

    # The next open finds the file free to change and converts — and it says
    # nothing, because there is nothing to say.
    reopened = Registry.open(db_path=db_path)
    assert attempts[1] == ("wal", None)
    assert reopened.journal_mode == "wal" == _journal_mode(db_path)
    assert [project.slug for project in reopened.list_projects()] == ["ops"]
    assert len(_registry_log_events(store_module.JOURNAL_MODE_DEGRADED)) == 1


def test_a_conversion_that_must_wait_a_reader_pays_the_whole_budget(tmp_path) -> None:
    """The other refusal shape: a reader in the way is *waited out*, then refused.

    What a refused conversion costs depends on what is in the way (see
    :func:`clear_record.service.store._write_ahead_log`): another writer's
    reservation is refused at once — the test above — while a **reader's** shared
    lock makes the pragma wait the connection's whole busy timeout before it
    fails, because the change needs exclusive access past that reader. Both
    shapes are the registry's contract, so both are pinned.

    This drives the seam directly rather than through an open, and that is
    deliberate: a reader held for the whole attempt also refuses the migration's
    COMMIT in the rollback journal, so an open under it cannot complete without a
    clock deciding which of the two waits the reader outlives. The wait is the
    box's budget, so this costs a fifth of a second rather than the policy's
    five, and the assertion is built to be true of the *mechanism* — SQLite's
    busy handler cannot return before its deadline — rather than of a machine's
    speed.
    """
    db_path = tmp_path / "registry.sqlite3"
    _registry(tmp_path).create_project("Ops", actor="console")
    _in_the_rollback_journal(db_path)
    engine = _engine_timing_out_box(db_path)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN")
    holder.execute("SELECT count(*) FROM project").fetchone()  # a reader's SHARED
    try:
        started = time.monotonic()
        mode, reason = store_module._write_ahead_log(engine)
        waited = time.monotonic() - started

        assert mode == "delete"  # it asked for the log and reports what it has
        assert reason and "lock" in reason, reason
        assert waited >= _BOX_TIMEOUT, (
            f"the pragma gave up after {waited:.2f}s: the reader was not waited out"
        )
        assert _journal_mode(db_path) == "delete"
    finally:
        holder.rollback()
        holder.close()

    # With the reader gone the same seam converts: what was measured is the
    # reader's effect, not a permanently refused file.
    assert store_module._write_ahead_log(engine) == ("wal", None)


def test_an_actor_outside_the_vocabulary_registers_nothing(tmp_path) -> None:
    """The registration reads the actor **before** it writes anything.

    The decorated creates get that gate from ``audit.recorded``'s wrapper, which
    runs it before the method body; this path composes the inserts itself, so the
    gate has to be its own — and it has to be *first*, before the writer lock.
    Left to the row-append, the gate would refuse **after** the registration had
    committed: a folder registered under an unknown actor with no row to say who
    registered it, which is the opposite of what the record is for (measured at
    the reviewed tip: one project, one meeting, an empty record).
    """
    registry = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()

    with pytest.raises(ValueError, match="unknown actor"):
        registry.meeting_for_workspace(str(workspace), actor="hacker")

    assert registry.list_projects() == []
    assert registry.list_meetings() == []
    assert registry.list_audit_events() == []


def test_every_connection_carries_the_decided_policy(tmp_path) -> None:
    """The connection policy is a promise about the *database*, not about a listener.

    SQLite keeps this state per connection and resets every new one to its own
    default, so "which pragmas are set" is only answerable by asking a connection
    the registry's own engine made — and the answer is the decision
    (:func:`clear_record.service.store._engine`), not the code that applies it:
    five seconds of waiting for a writer, a commit that is synced before it is
    acknowledged, and the two guards the audit record leans on. Asked of two
    connections, because the promise is about every one of them.
    """
    registry = _registry(tmp_path)

    for _ in range(2):
        with registry._engine.connect() as conn:
            assert {
                pragma: conn.exec_driver_sql(f"PRAGMA {pragma}").scalar()
                for pragma in _CONNECTION_POLICY
            } == _CONNECTION_POLICY


def test_a_second_writer_waits_out_a_held_lock_and_then_succeeds(tmp_path) -> None:
    """The busy timeout is a *wait*, and the write that waited lands afterwards.

    The registration takes the writer lock before its first read
    (:meth:`Registry._writer_session`), so a lock another surface holds makes it
    wait rather than look and collide. The lock is released a moment before the
    policy's budget runs out, and what comes back is the registration — one
    meeting, registered once. (The *other* end of the budget — contention that
    outlasts it — is ``test_prolonged_contention_fails_within_the_budget``.)
    """
    registry = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()
    holder = sqlite3.connect(str(registry.db_path), timeout=0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Timer(0.05, _release_lock, args=(holder,))
    released.start()
    try:
        meeting = registry.meeting_for_workspace(str(workspace), actor="console")
    finally:
        released.join(timeout=10)
        _release_lock(holder)

    assert meeting.slug == "second"
    assert [meeting.slug for meeting in registry.list_meetings()] == ["second"]


def test_prolonged_contention_fails_within_the_budget(tmp_path, monkeypatch) -> None:
    """A writer that never gets the lock is refused **after the budget**, not left waiting.

    The policy pins the budget at 5000 ms — asserted, as a fact about a
    connection, by ``test_every_connection_carries_the_decided_policy``; the
    engine here is the short-timeout box so that *this* test waits the box's own
    fifth of a second instead of five seconds of the suite's, and what it checks
    is that the wait is the configured one and that the end of it is the driver's
    refusal (``OperationalError``, the class every caller of this store already
    handles) rather than a success or a hang. The registration is the operation:
    it takes the lock before it reads anything, so there is no path on which it
    reads around a held lock and collides instead.
    """
    monkeypatch.setattr(store_module, "_engine", _engine_timing_out_box)
    registry = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()
    holder = sqlite3.connect(str(registry.db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError, match="database is locked"):
            registry.meeting_for_workspace(str(workspace), actor="console")
        waited = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert _BOX_TIMEOUT <= waited < 5 * _BOX_TIMEOUT, (
        f"the refusal took {waited:.2f}s, which is not the configured budget"
    )
    # Refused, and nothing half-written: the transaction rolled back with it.
    assert registry.list_projects() == []
    assert registry.list_meetings() == []
    # ... and refused *audibly*: the row the creates owe cannot be written while
    # the lock is held, so the loss is stated under the audit record's own name
    # for it. The verb is the create that was in flight — the folder's project,
    # the operation's first — and the vocabulary is unchanged.
    assert [
        (record["actor"], record["action"], record["outcome"])
        for record in _registry_log_events("audit.row_lost")
    ] == [("console", "project.create", "failed")]


# --- the registration's lookup and its rows -------------------------------- #
def test_a_re_registration_resolves_no_stored_workspace_path(
    tmp_path, monkeypatch
) -> None:
    """The registration's hot path does no filesystem work **inside** the writer lock.

    The lookup runs under the lock, so the resolving comparison
    (:meth:`Registry._meeting_at`) must not be its common path: this node stores
    the **resolved** path when it registers a folder, so the exact comparison
    (:meth:`Registry._meeting_at_exact`) answers the second ``run <dir>`` from
    one row comparison, and no stored path is resolved at all. Measured before
    the split: 201 ``Path.resolve`` calls and about a second inside the lock for
    a registry of 200 meetings, which is exactly the filesystem work a
    read-then-write operation must keep out of its transaction.

    Counting the resolver keeps that a fact about the seam rather than a claim in
    a docstring: the input is resolved **once** (outside the lock, by the
    caller's own step) and the scan — which resolves every stored path — is not
    reached.
    """
    registry = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()
    registered = registry.meeting_for_workspace(str(workspace), actor="console")

    calls: list[str] = []
    real_resolve = store_module._resolved_workspace

    def counting(path: str) -> str:
        calls.append(path)
        return real_resolve(path)

    monkeypatch.setattr(store_module, "_resolved_workspace", counting)
    again = registry.meeting_for_workspace(str(workspace), actor="console")

    assert again.id == registered.id
    assert calls == [str(workspace)], "a stored path was resolved again"


def test_a_workspace_stored_under_another_spelling_is_the_same_folder(
    tmp_path,
) -> None:
    """The fallback arm: a spelling a *surface* stored still answers as this folder.

    Registration stores the resolved path, so the exact comparison is the common
    path — but the console and the API take a workspace path as a caller typed
    it, and a registry can therefore hold a meeting whose ``workspace_path`` is a
    spelling rather than the resolved path. The promise ("a relative,
    trailing-separator or symlinked spelling is the same workspace") is what the
    resolving arm is for, and it runs exactly when the exact comparison misses —
    which is what keeps the decision the same while the syscalls stay off the hot
    path.
    """
    registry = _registry(tmp_path)
    real = tmp_path / "second"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    registry.create_project("Ops", actor="console")
    stored = registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(link), actor="console"
    )

    found = registry.meeting_for_workspace(str(real), actor="cli")

    assert found.id == stored.id, "the spelling was not read as the same workspace"
    assert len(registry.list_meetings()) == 1, "the lookup registered a second meeting"


# --- a folder registered by two surfaces at once ---------------------------- #
def test_two_registrations_of_one_folder_leave_one_meeting(
    tmp_path, monkeypatch
) -> None:
    """Registering one folder twice at once is one meeting, and the loser registers nothing.

    Two surfaces registering one folder are a real pair — the command line's
    ``run <dir>`` and the console adding the same folder — and what makes them
    one meeting is the writer lock the registration takes **before its first
    read**: the loser's own transaction cannot begin until the winner's has
    committed, so the loser's scan reads the winner's row and it creates nothing
    at all.

    The winner is paused *inside* its transaction here — its row flushed, nothing
    committed — so the loser's wait is against a lock that is not going away
    until this test says so, and there is no interleaving in which the loser
    reads around the lock and collides instead. That is what the earlier form of
    this test injected (a winner landing in the loser's window between the scan
    and the insert) and what the lock removes: the window it fired into no longer
    exists, so it is the *wait* that has to be fenced rather than a moment inside
    the loser. The observable is the record, not a timing: both calls answer with
    one meeting, and there is exactly **one** ``meeting.create`` row, because the
    loser's own row would describe a meeting this folder does not have.
    """
    winner = _registry(tmp_path)
    loser = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()
    in_the_transaction = threading.Event()
    released = threading.Event()
    real_insert = winner._insert_meeting

    def the_winner_holds_its_lock(*args, **kwargs):
        row = real_insert(*args, **kwargs)
        in_the_transaction.set()
        assert released.wait(timeout=10), "the lock was never released"
        return row

    monkeypatch.setattr(winner, "_insert_meeting", the_winner_holds_its_lock)
    answered: list[tuple[int, str]] = []
    failures: list[BaseException] = []

    def the_winner_registers() -> None:
        try:
            meeting = winner.meeting_for_workspace(str(workspace), actor="console")
            answered.append((meeting.id, meeting.slug))
        except BaseException as exc:  # the failure this test is not about
            failures.append(exc)

    def the_late_registration() -> None:
        try:
            meeting = loser.meeting_for_workspace(str(workspace), actor="api")
            answered.append((meeting.id, meeting.slug))
        except BaseException as exc:
            failures.append(exc)

    winner_thread = threading.Thread(target=the_winner_registers, daemon=True)
    late_thread = threading.Thread(target=the_late_registration, daemon=True)
    winner_thread.start()
    assert in_the_transaction.wait(timeout=10), "the winner never got its lock"
    late_thread.start()
    time.sleep(0.05)  # long enough for the late call to reach the lock
    assert not failures and not answered, "the late call did not wait"
    released.set()
    winner_thread.join(timeout=10)
    late_thread.join(timeout=10)

    assert not winner_thread.is_alive() and not late_thread.is_alive()
    assert not failures, failures[0]
    one = winner.list_meetings()
    assert len(one) == 1, "one folder, one meeting"
    assert answered == [(one[0].id, "second"), (one[0].id, "second")]
    assert [(row.action, row.target) for row in winner.list_audit_events()] == [
        ("project.create", "project:second"),
        ("meeting.create", "meeting:second"),
    ]
    # The loser wrote no rows of its own: it registered nothing (it reads the
    # same file, so this is the same record).
    assert loser.list_audit_events() == winner.list_audit_events()


def test_a_registration_that_fails_after_the_project_leaves_nothing(
    tmp_path, monkeypatch
) -> None:
    """Half a registration is not a registration: the project rolls back with the meeting.

    The failure is injected **inside** the registration's transaction and *after*
    the project create — the shape a disk that fills up mid-operation has, and
    the shape the base could not survive: there the project was a unit of work of
    its own, so a meeting create that then failed left the folder half-registered
    (measured at ``b648294``: one ``project`` row, no meeting, and the ``ok`` row
    the project's create had already written). One transaction is what closes it.

    What the rollback must **not** take with it is the record of the refusal: the
    ``ok`` rows belong to committed work and are not written, and one ``failed``
    row takes their place — the same row the base's decorated create would have
    appended, under the same verb and the same target — because a registration
    that failed is exactly what an audit record exists for. A caller therefore
    sees a refusal and a record of it, and never a folder that was registered
    without one.

    The second half is the point of the first: with no residue, the folder is
    registered for the first time by whoever asks next, under its own slug.
    """
    registry = _registry(tmp_path)
    workspace = tmp_path / "second"
    workspace.mkdir()

    def the_disk_fills_up(*args, **kwargs):
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(registry, "_insert_meeting", the_disk_fills_up)
    with pytest.raises(RuntimeError, match="no space left"):
        registry.meeting_for_workspace(str(workspace), actor="console")

    assert registry.list_projects() == []
    assert registry.list_meetings() == []
    assert [
        (row.action, row.target, row.outcome) for row in registry.list_audit_events()
    ] == [("meeting.create", "meeting:second", "failed")]

    monkeypatch.undo()  # the injection is over: the directory is registrable
    meeting = registry.meeting_for_workspace(str(workspace), actor="console")
    assert (meeting.slug, meeting.workspace_path) == ("second", str(workspace))
    assert [
        (row.action, row.target, row.outcome) for row in registry.list_audit_events()
    ] == [
        # The refused attempt first (the refusal is recorded when it happens),
        # then the registration that succeeded.
        ("meeting.create", "meeting:second", "failed"),
        ("project.create", "project:second", "ok"),
        ("meeting.create", "meeting:second", "ok"),
    ]


def test_a_folders_own_slug_taken_by_another_folder_is_computed_again(
    tmp_path,
) -> None:
    """Two folders of one name: the second gets a meeting of its own, under a computed slug.

    A project's meetings share one slug space, so registering ``a/second`` and
    then ``b/second`` pins ``second`` twice and the second insert collides. That
    is **not** the same folder's race — the lock cannot settle it, since both
    registrations are legitimate — so the pinned attempt is given up and the
    create computes ``second-2``, which is what this case has always done. The
    registration holds its transaction across the collision: the attempt runs
    inside a savepoint, so the collision costs that attempt and not the project
    (or the folder's own registration) it is part of.

    Both folders are registered, each with one meeting, and the folder that
    answers a repeat call is the folder's own.
    """
    registry = _registry(tmp_path)
    first = tmp_path / "a" / "second"
    second = tmp_path / "b" / "second"
    for path in (first, second):
        path.mkdir(parents=True)

    one = registry.meeting_for_workspace(str(first), actor="console")
    two = registry.meeting_for_workspace(str(second), actor="console")

    assert (one.slug, two.slug) == ("second", "second-2")
    assert one.id != two.id
    assert registry.meeting_for_workspace(str(second), actor="api").id == two.id
    assert registry.meeting_for_workspace(str(first), actor="api").id == one.id
    assert len(registry.list_meetings()) == 2
    assert [(row.action, row.target) for row in registry.list_audit_events()] == [
        ("project.create", "project:second"),
        ("meeting.create", "meeting:second"),
        ("meeting.create", "meeting:second"),
    ]
