"""Registry behaviour: projects and the multi-project glossary table.

These exercise the service seam directly (external behaviour, temp DB — no web
app, no network), so the store is trusted independently of any adapter.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, event

from clear_record.service import Registry, entities
from clear_record.service.store import _alembic_config


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _registry_at(db_path: Path, revision: str) -> None:
    """Build a registry at one revision of its schema's history.

    The retired ladder's steps are the revisions now, so an older registry is
    made the way this build makes one: by upgrading to that revision.
    """
    command.upgrade(_alembic_config(db_path), revision)


def _ladder_left_it(db_path: Path, version: int) -> None:
    """Give a registry the shape the retired ladder left behind at one version.

    The ladder kept its version in ``schema_version`` and knew nothing of
    Alembic, so a registry from a release before this build looks like this.
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE alembic_version")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _version_tables(db_path: Path) -> list[str]:
    """Which version states a registry carries: the ladder's and Alembic's."""
    with sqlite3.connect(str(db_path)) as conn:
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
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?", (name,)
        ).fetchone()
    return _normalized(row[0]) if row else None


def _seed_project(db_path: Path) -> None:
    """A project row as an existing registry would already have one."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )


def test_create_and_list_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    project = reg.create_project("Weekly Ops", notes="ops sync")

    assert project.slug == "weekly-ops"
    assert project.name == "Weekly Ops"
    assert project.notes == "ops sync"
    assert reg.list_projects() == [project]
    assert reg.get_project("weekly-ops") == project
    assert reg.get_project("nope") is None


def test_slug_collision_gets_a_suffix(tmp_path) -> None:
    reg = _registry(tmp_path)
    assert (reg.create_project("Sync").slug, reg.create_project("Sync").slug) == (
        "sync",
        "sync-2",
    )


def test_duplicate_explicit_slug_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("A", slug="shared")
    with pytest.raises(ValueError):
        reg.create_project("B", slug="shared")


def test_unsafe_explicit_slugs_are_rejected(tmp_path) -> None:
    """A caller-supplied slug must be [a-z0-9-]+; the managed root builds paths from it."""
    reg = _registry(tmp_path)
    for bad in ("../escaped", "a/b", "UPPER", "with space", "."):
        with pytest.raises(ValueError, match="slug must match"):
            reg.create_project("Ops", slug=bad)

    reg.create_project("Ops")
    with pytest.raises(ValueError, match="slug must match"):
        reg.create_meeting("ops", "Kickoff", slug="../escaped")


def test_blank_project_name_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(ValueError):
        reg.create_project("   ")


def test_state_survives_reopen(tmp_path) -> None:
    db = tmp_path / "registry.sqlite3"
    Registry(db).create_project("Persist")
    assert [p.slug for p in Registry(db).list_projects()] == ["persist"]


def test_update_project(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    updated = reg.update_project("ops", name="Ops Weekly", notes="n")
    assert (updated.name, updated.notes) == ("Ops Weekly", "n")
    with pytest.raises(KeyError):
        reg.update_project("missing", name="x")


def test_glossary_lifecycle(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    term = reg.add_term(
        "ops", "李工", reading="Li Gong", aliases="老李", definition="lead"
    )
    assert term.status == "candidate" and term.added_by == "human"
    assert reg.list_terms("ops") == [term]

    assert reg.update_term(term.id, status="confirmed").status == "confirmed"
    assert reg.update_term(term.id, definition="team lead").definition == "team lead"

    reg.delete_term(term.id)
    assert reg.list_terms("ops") == []
    with pytest.raises(KeyError):
        reg.delete_term(term.id)


def test_duplicate_term_in_one_project_is_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    reg.add_term("ops", "Falcon")
    with pytest.raises(ValueError):
        reg.add_term("ops", "Falcon")


def test_same_term_is_allowed_in_two_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    reg.create_project("Research")
    reg.add_term("ops", "Falcon")
    reg.add_term("research", "Falcon")
    assert len(reg.list_terms()) == 2


def test_cross_project_table_filters_by_status_and_project(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    reg.create_project("Research")
    reg.add_term("ops", "Alpha", status="confirmed")
    reg.add_term("ops", "Beta", status="candidate")
    reg.add_term("research", "Gamma", status="confirmed")

    assert [t.term for t in reg.list_terms(status="confirmed")] == ["Alpha", "Gamma"]
    assert [t.term for t in reg.list_terms("ops")] == ["Alpha", "Beta"]
    assert [t.term for t in reg.list_terms("ops", status="confirmed")] == ["Alpha"]
    assert {t.project_slug for t in reg.list_terms()} == {"ops", "research"}


def test_term_counts_include_empty_projects(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    reg.create_project("Research")
    reg.add_term("ops", "A")
    reg.add_term("ops", "B")
    assert reg.term_counts() == {"ops": 2, "research": 0}


def test_invalid_status_and_author_are_rejected(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    with pytest.raises(ValueError):
        reg.add_term("ops", "A", status="maybe")
    with pytest.raises(ValueError):
        reg.add_term("ops", "A", added_by="robot")
    with pytest.raises(KeyError):
        reg.add_term("nope", "A")


def test_agent_terms_are_marked_as_such(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    term = reg.add_term("ops", "Falcon", added_by="agent")
    assert term.added_by == "agent"
    assert term.status == "candidate"


def test_meeting_lifecycle_and_tape_set(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    meeting = reg.create_meeting("ops", "Kickoff", recorded_at="2026-09-14")
    assert meeting.slug == "kickoff"
    assert meeting.status == "new"
    assert meeting.notes == ""
    assert reg.list_meetings("ops") == [meeting]

    assert reg.create_meeting("ops", "Kickoff").slug == "kickoff-2"
    assert reg.get_meeting("ops", "kickoff") == meeting
    assert reg.get_meeting("ops", "nope") is None

    selected = reg.set_recording_set(meeting.id, ["/a.wav", "/b.wav"])
    assert selected.paths == ("/a.wav", "/b.wav")
    assert reg.latest_recording_set(meeting.id) == selected

    # A newer selection supersedes the old one.
    newer = reg.set_recording_set(meeting.id, ["/c.wav"])
    assert reg.latest_recording_set(meeting.id) == newer

    with pytest.raises(ValueError):
        reg.set_recording_set(meeting.id, [])


def test_update_meeting_notes_and_title(tmp_path) -> None:
    """The story has a durable home: meeting notes (and title) are writable."""
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    meeting = reg.create_meeting("ops", "Kickoff")

    updated = reg.update_meeting(meeting.id, notes="tell the story here")
    assert updated.notes == "tell the story here"
    assert reg.get_meeting("ops", "kickoff").notes == "tell the story here"

    assert reg.update_meeting(meeting.id, notes="").notes == ""
    assert reg.update_meeting(meeting.id, title="Kickoff v2").title == "Kickoff v2"

    with pytest.raises(ValueError):
        reg.update_meeting(meeting.id, title="   ")
    with pytest.raises(KeyError):
        reg.update_meeting(999, notes="nope")


def test_run_and_artifact_rows(tmp_path) -> None:
    reg = _registry(tmp_path)
    reg.create_project("Ops")
    meeting = reg.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))

    run = reg.create_run(meeting.id, backend="apple", model="small")
    assert run.status == "queued"
    assert reg.get_run(run.id) == run
    assert [r.id for r in reg.list_runs(meeting.id)] == [run.id]

    updated = reg.update_run(run.id, status="running", started_at="now")
    assert updated.status == "running"
    with pytest.raises(ValueError):
        reg.update_run(run.id, status="bogus")

    # RUN-02: origin is one of the four surfaces, and only a start path has one.
    assert run.origin is None
    with pytest.raises(ValueError):
        reg.create_run(meeting.id, origin="grafana")
    # A meeting carries one active run (revision 0009's index), so the run the
    # origin is read back from is the meeting's next one.
    reg.update_run(run.id, status="done")
    assert reg.create_run(meeting.id, origin="cli").origin == "cli"

    artifact = reg.add_artifact(
        meeting.id, run_id=run.id, kind="record", path="/ws/record.json", sha256="ab"
    )
    assert artifact.kind == "record"
    assert artifact.produced_by == "pipeline"
    assert reg.list_artifacts(meeting.id) == [artifact]

    assert reg.set_meeting_status(meeting.id, "recorded").status == "recorded"
    with pytest.raises(ValueError):
        reg.set_meeting_status(meeting.id, "bogus")


def test_a_registry_at_revision_one_gains_every_later_revision(tmp_path) -> None:
    """An existing registry at the first revision gains the later ones on open."""
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0001")
    _seed_project(db)

    reg = Registry(db)
    assert [p.slug for p in reg.list_projects()] == ["ops"]
    assert reg.create_meeting("ops", "Kickoff").slug == "kickoff"


def test_a_registry_at_revision_three_gains_every_later_revision(tmp_path) -> None:
    """An existing registry at revision three gains every later one on open."""
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0003")
    _seed_project(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO meeting (project_id, slug, title, status, created_at)"
            " VALUES (1, 'kickoff', 'Kickoff', 'new', 'now')"
        )

    reg = Registry(db)
    meeting = reg.get_meeting("ops", "kickoff")
    assert meeting is not None and meeting.notes == ""
    assert reg.update_meeting(meeting.id, notes="story").notes == "story"
    # Revision 0005: an uploaded tape can be recorded and joins the meeting's tape set.
    tape = reg.register_tape(meeting.id, path="/tapes/a.wav", sha256="0" * 64, bytes=3)
    assert reg.list_tapes(meeting.id) == [tape]
    assert reg.latest_recording_set(meeting.id).paths == ("/tapes/a.wav",)
    # Revision 0006: a run carries its durable options and its persisted event stream.
    run = reg.create_run(meeting.id, run_options={"backend": "apple"})
    assert reg.get_run(run.id).run_options == {"backend": "apple"}
    assert reg.count_run_events(run.id) == 0
    # Revision 0007: a run records where it came from, and the claim records its
    # owner. The claim runs on a *second* meeting's run: one meeting has one
    # active run, which revision 0009's index enforces.
    assert reg.get_run(run.id).origin is None  # a seeded row has no origin
    second = reg.create_meeting("ops", "Second pass")
    queued = reg.create_run(second.id, origin="console")
    claimed = reg.claim_run(queued.id, owner="peer:1")
    assert claimed is not None
    assert (claimed.origin, claimed.owner) == ("console", "peer:1")
    assert claimed.heartbeat_at is not None
    # Revision 0008: a run can be linked to the run it resumes, and carry a cancel request.
    assert claimed.resumes_run_id is None and claimed.cancel_requested_at is None
    with pytest.raises(KeyError):
        reg.create_run(meeting.id, resumes_run_id=999)  # no such run
    assert reg.request_cancel(claimed.id).cancel_requested_at is not None
    assert reg.cancel_requested(claimed.id) is True
    # A queued run is cancelled outright: it never reaches a pipeline.
    stopped = reg.stop_run(run.id, ended_at="now", progress={})
    assert stopped is not None and stopped.status == "stopped"
    # And a run that is not queued any more is left to its owner.
    assert reg.stop_run(claimed.id, ended_at="now", progress={}) is None
    # Revision 0009: the meeting's run has ended, so the meeting runs again — and
    # the new run continues the stopped one.
    resumed = reg.create_run(meeting.id, resumes_run_id=run.id)
    assert resumed.resumes_run_id == run.id


def test_a_registry_from_the_retired_ladder_migrates_on_open(tmp_path) -> None:
    """A registry the ladder left behind opens, keeps its rows, and is current.

    This is the registry an upgrading user has: the ladder's tables with its
    ``schema_version`` row and no revision recorded. Opening it must place it at
    that version — not re-run the revisions it already has — and run the rest.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0003")
    _ladder_left_it(db, 3)
    _seed_project(db)

    reg = Registry(db)
    assert [p.slug for p in reg.list_projects()] == ["ops"]
    # Every later revision is in place: notes (0004), tapes (0005), a run's
    # durable options (0006) and its ownership (0007) all answer.
    meeting = reg.create_meeting("ops", "Kickoff")
    assert reg.update_meeting(meeting.id, notes="story").notes == "story"
    tape = reg.register_tape(meeting.id, path="/tapes/a.wav", sha256="0" * 64, bytes=3)
    assert reg.list_tapes(meeting.id) == [tape]
    assert reg.create_run(meeting.id, origin="cli").origin == "cli"


def test_a_registry_recording_a_newer_revision_fails_loudly(tmp_path) -> None:
    """A revision this build does not carry is refused, not half-read."""
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0008")
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '9999'")

    with pytest.raises(
        RuntimeError, match="9999 is not one this build carries.*upgrade clear-record"
    ):
        Registry(db)

    # The refusal ran nothing: the registry still records what it recorded.
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "9999",
        )


def test_a_ladder_registry_from_a_newer_version_fails_loudly(tmp_path) -> None:
    """The guard the retired ladder enforced survives: a newer version is refused."""
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0008")
    _ladder_left_it(db, 99)

    with pytest.raises(
        RuntimeError, match="99 is newer than this build carries.*upgrade clear-record"
    ):
        Registry(db)

    # Nothing was applied and nothing was stamped: the refusal comes first.
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (99,)
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

    assert reg.create_project("Ops").slug == "ops"
    assert db.exists()
    assert not (tmp_path / "what").exists()  # the file the text form creates


def test_a_migrated_ladder_registry_still_reads_for_an_older_build(tmp_path) -> None:
    """A build from before Alembic must still be able to read what this one migrates.

    The ladder's row is the only version state such a build reads, and the head
    revision's schema is the ladder's own last one, so the row is left at the
    head's number: a build comparing it with its own version finds them equal and
    opens the registry, instead of running DDL it already has.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0005")
    _ladder_left_it(db, 5)

    Registry(db)

    # The head's number, which is what the row has to say (`0005` -> `0009`).
    head = ScriptDirectory.from_config(_alembic_config(db)).get_current_head()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (
            int(head),
        )

    # The other side of the same boundary: a registry *this* build creates
    # carries no such record at all, so a build from before Alembic runs its
    # ladder there and stops at the first step that is not idempotent (the notes
    # column). Nothing the app owns is changed before it does.
    fresh = tmp_path / "fresh.sqlite3"
    Registry(fresh)
    assert _version_tables(fresh) == ["alembic_version"]


# --- the mapping the registry reads and writes through (ADR-0030) ----------- #


def test_the_mapping_describes_every_column_and_uniqueness_the_revisions_own(
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
    table, every column with its type and nullability, and every declared
    uniqueness, against the head revision's own schema.

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
    with sqlite3.connect(str(db)) as conn:
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


def test_the_active_run_index_arrives_with_revision_0009(tmp_path) -> None:
    """The database's one-active-run rule is a revision, and it refuses a row.

    A registry at 0008 — every release before this one — carries no such index,
    and opening it, which is what migrates it, is what gives it one: the same
    index a registry created fresh carries, because both are the DDL revision
    0009 ran. The rule is then exercised the way a second *writer* exercises it:
    one connection, one bare INSERT, no service call in the way.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0008")
    assert _index_sql(db, "pipeline_run_active_meeting") is None

    reg = Registry(db)  # migrates 0008 → 0009
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
    reg.create_project("Ops")
    meeting = reg.create_meeting("ops", "Kickoff")
    active = reg.create_run(meeting.id, origin="console")
    with sqlite3.connect(str(db)) as conn:
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
    before it creates the index: one active run per meeting survives (a
    ``running`` row over a ``queued`` one, else the oldest), the rest end as
    ``interrupted`` with the reason the user reads on the row, and a meeting that
    holds a single active run — or none — is left exactly as it was. The registry
    is current afterwards, and the rule bites from there on.
    """
    db = tmp_path / "registry.sqlite3"
    _registry_at(db, "0008")
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )
        for slug in ("kickoff", "standup", "retro"):
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
                # ... and a running run with a queued one behind it.
                (2, "queued", None, None),
                (2, "running", "now", "host:1"),
                # What the rule wants: one active run, and a run that had ended.
                (3, "queued", None, None),
                (3, "done", "now", None),
            ),
        )

    reg = Registry(db)

    # The oldest of the two queued runs is the meeting's; the later one is ended,
    # with the reason written where the console reads an interrupted run's why.
    keeper, loser = sorted(reg.list_runs(1), key=lambda run: run.id)
    assert (keeper.id, keeper.status, keeper.ended_at, keeper.error) == (
        1,
        "queued",
        None,
        None,
    )
    assert (loser.id, loser.status) == (2, "interrupted")
    assert loser.ended_at is not None
    assert loser.error == (
        "this meeting had more than one active run, and one run per meeting is "
        "what the registry keeps"
    )

    # A running row is the meeting's one, whatever a queued row behind it says.
    waiting, running = sorted(reg.list_runs(2), key=lambda run: run.id)
    assert (waiting.id, waiting.status) == (3, "interrupted")
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

    assert _index_sql(db, "pipeline_run_active_meeting") is not None
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0009",
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
    reg.create_project("Ops")
    meeting = reg.create_meeting("ops", "Kickoff")
    run = reg.create_run(meeting.id)

    seen: list[tuple[str, tuple]] = []

    @event.listens_for(reg._engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, parameters, _context, _many) -> None:
        seen.append((" ".join(statement.split()), parameters))

    claimed = reg.claim_run(run.id, owner="host:1")
    assert claimed is not None and claimed.status == "running"
    assert len(seen) == 2, seen  # the transition, and the row it wrote read back
    statement, parameters = seen[0]
    assert statement.startswith("UPDATE pipeline_run")
    assert "EXISTS" in statement.upper()  # the node's rule, inside the WHERE
    assert "host:1" in parameters and run.id in parameters

    seen.clear()
    reaped = reg.interrupt_run(
        run.id, observed=claimed, ended_at="now", error="gone", progress={}
    )
    assert reaped is not None and reaped.status == "interrupted"
    assert len(seen) == 2, seen
    statement, parameters = seen[0]
    assert statement.startswith("UPDATE pipeline_run")
    assert claimed.owner in parameters and claimed.heartbeat_at in parameters

    # The behaviour the shape buys: the same snapshot again is a stale one, and
    # a stale observation does not interrupt a row it no longer describes.
    assert (
        reg.interrupt_run(
            run.id, observed=claimed, ended_at="later", error="stale", progress={}
        )
        is None
    )
