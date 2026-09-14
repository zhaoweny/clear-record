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
