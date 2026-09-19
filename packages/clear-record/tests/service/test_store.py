"""Registry behaviour: projects and the multi-project glossary table.

These exercise the service seam directly (external behaviour, temp DB — no web
app, no network), so the store is trusted independently of any adapter.
"""

from __future__ import annotations

import pytest

from clear_record.service import Registry


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


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


def test_v1_registry_upgrades_forward(tmp_path) -> None:
    """An existing v1 database gains the v2 tables on open (forward-only)."""
    import sqlite3

    from clear_record.service.store import _SCHEMA_V1

    db = tmp_path / "registry.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_V1)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute(
        "INSERT INTO project (slug, name, notes, created_at) VALUES ('ops', 'Ops', '', 'now')"
    )
    conn.commit()
    conn.close()

    reg = Registry(db)
    assert [p.slug for p in reg.list_projects()] == ["ops"]
    assert reg.create_meeting("ops", "Kickoff").slug == "kickoff"


def test_v3_registry_gains_meeting_notes_and_tapes(tmp_path) -> None:
    """An existing v3 database gains every later column and table on open."""
    import sqlite3

    from clear_record.service.store import (
        SCHEMA_VERSION,
        _SCHEMA_V1,
        _SCHEMA_V2,
        _SCHEMA_V3,
    )

    db = tmp_path / "registry.sqlite3"
    conn = sqlite3.connect(str(db))
    for ddl in (_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3):
        conn.executescript(ddl)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    conn.execute(
        "INSERT INTO project (slug, name, notes, created_at)"
        " VALUES ('ops', 'Ops', '', 'now')"
    )
    conn.execute(
        "INSERT INTO meeting (project_id, slug, title, status, created_at)"
        " VALUES (1, 'kickoff', 'Kickoff', 'new', 'now')"
    )
    conn.commit()
    conn.close()

    reg = Registry(db)
    assert SCHEMA_VERSION == 8
    meeting = reg.get_meeting("ops", "kickoff")
    assert meeting is not None and meeting.notes == ""
    assert reg.update_meeting(meeting.id, notes="story").notes == "story"
    # v5: an uploaded tape can be recorded and joins the meeting's tape set.
    tape = reg.register_tape(meeting.id, path="/tapes/a.wav", sha256="0" * 64, bytes=3)
    assert reg.list_tapes(meeting.id) == [tape]
    assert reg.latest_recording_set(meeting.id).paths == ("/tapes/a.wav",)
    # v6: a run carries its durable options and its persisted event stream.
    run = reg.create_run(meeting.id, run_options={"backend": "apple"})
    assert reg.get_run(run.id).run_options == {"backend": "apple"}
    assert reg.count_run_events(run.id) == 0
    # v7: a run records where it came from, and the claim records its owner.
    assert reg.get_run(run.id).origin is None  # a seeded row has no origin
    queued = reg.create_run(meeting.id, origin="console")
    claimed = reg.claim_run(queued.id, owner="peer:1")
    assert claimed is not None
    assert (claimed.origin, claimed.owner) == ("console", "peer:1")
    assert claimed.heartbeat_at is not None
    # v8: a run can be linked to the run it resumes, and carry a cancel request.
    assert claimed.resumes_run_id is None and claimed.cancel_requested_at is None
    resumed = reg.create_run(meeting.id, resumes_run_id=run.id)
    assert resumed.resumes_run_id == run.id
    with pytest.raises(KeyError):
        reg.create_run(meeting.id, resumes_run_id=999)  # no such run
    assert reg.request_cancel(claimed.id).cancel_requested_at is not None
    assert reg.cancel_requested(claimed.id) is True
    # A queued run is cancelled outright: it never reaches a pipeline.
    stopped = reg.stop_run(run.id, ended_at="now", progress={})
    assert stopped is not None and stopped.status == "stopped"
    # And a run that is not queued any more is left to its owner.
    assert reg.stop_run(claimed.id, ended_at="now", progress={}) is None
