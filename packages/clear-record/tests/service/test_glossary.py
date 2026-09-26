"""Project glossary snapshots: the registry → workspace ``glossary.txt`` bridge.

The snapshot is the decoder's initial prompt; its two guarantees are what make
the tuning loop trustworthy — only *confirmed* terms reach it, and its identity
(text + sha256) is stable and order-independent so a run can record it.
"""

from __future__ import annotations

from pathlib import Path

from clear_record.pipeline.workspace import Workspace
from clear_record.service import (
    GlossaryTerm,
    Registry,
    build_snapshot,
    canonical_terms,
    project_snapshot,
    snapshot_from_text,
    write_project_snapshot,
    write_snapshot,
)


def _term(term: str, status: str = "confirmed") -> GlossaryTerm:
    return GlossaryTerm(
        id=0,
        project_id=0,
        project_slug="ops",
        term=term,
        reading=None,
        aliases=None,
        definition=None,
        status=status,
        added_by="human",
        notes=None,
        created_at="",
    )


def _registry(tmp_path: Path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def test_snapshot_text_and_hash_are_stable_and_order_independent() -> None:
    """The same set of terms renders the same text and hash in any order."""
    first = build_snapshot([_term("Falcon"), _term("Aero"), _term("Booster")])
    shuffled = build_snapshot([_term("Booster"), _term("Falcon"), _term("Aero")])

    assert first.terms == ("Aero", "Booster", "Falcon")
    assert first.text == "Aero\nBooster\nFalcon\n"  # one term per line
    assert first.sha256 == shuffled.sha256
    assert len(first.sha256) == 64
    assert not first.empty


def test_an_edited_glossary_changes_the_hash() -> None:
    before = build_snapshot([_term("Falcon")])
    after = build_snapshot([_term("Falcon"), _term("Booster")])

    assert before.sha256 != after.sha256
    assert before.text != after.text


def test_candidates_and_retired_terms_are_excluded() -> None:
    """An unreviewed agent suggestion must not bias the decoder."""
    snapshot = build_snapshot(
        [
            _term("Confirmed", status="confirmed"),
            _term("Draft", status="candidate"),
            _term("Old", status="retired"),
        ]
    )

    assert snapshot.terms == ("Confirmed",)
    assert snapshot.text == "Confirmed\n"


def test_canonical_terms_strips_dedupes_and_sorts_case_insensitively() -> None:
    assert canonical_terms(["  b ", "A", "", "a", "B", "  "]) == ("A", "a", "B", "b")


def test_snapshot_from_text_ignores_comments_and_blanks() -> None:
    snapshot = snapshot_from_text("# comment\nFalcon\n\n  Aero  \n# trailing\n")

    assert snapshot.terms == ("Aero", "Falcon")
    assert snapshot.text == "Aero\nFalcon\n"


def test_project_snapshot_and_writer_produce_the_pipeline_file(tmp_path: Path) -> None:
    """The registry's confirmed terms land in the workspace ``glossary.txt``."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    confirmation = registry.add_term("ops", "Falcon", status="confirmed")
    registry.add_term("ops", "Draft", added_by="agent")  # candidate
    workspace = Workspace.at(tmp_path / "ws")

    path, snapshot = write_project_snapshot(registry, "ops", workspace)

    assert path == workspace.glossary_path
    assert path.read_text(encoding="utf-8") == "Falcon\n"
    assert workspace.glossary_terms() == ["Falcon"]
    assert snapshot == project_snapshot(registry, "ops")
    assert snapshot.sha256 == build_snapshot([confirmation]).sha256

    # A second confirmed term changes the written file and the hash.
    registry.add_term("ops", "Booster", status="confirmed")
    _, edited = write_project_snapshot(registry, "ops", workspace)
    assert workspace.glossary_terms() == ["Booster", "Falcon"]
    assert edited.sha256 != snapshot.sha256


def test_retiring_a_term_empties_the_projects_snapshot(tmp_path: Path) -> None:
    """A retired term is gone from the snapshot the decoder reads.

    Retiring is not a row delete, so the registry still answers for the term —
    but the snapshot (and the ``glossary.txt`` a run writes) carries no term.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    term = registry.add_term("ops", "Falcon", status="confirmed")
    workspace = Workspace.at(tmp_path / "ws")
    path, _ = write_project_snapshot(registry, "ops", workspace)
    assert path.read_text(encoding="utf-8") == "Falcon\n"

    retired = registry.retire_term(term.id)
    path, snapshot = write_project_snapshot(registry, "ops", workspace)

    assert retired.status == "retired"
    assert snapshot.empty
    assert path.read_text(encoding="utf-8") == ""
    assert workspace.glossary_terms() == []
    assert registry.list_terms("ops") == [retired]


def test_write_snapshot_is_atomic_and_overwrites(tmp_path: Path) -> None:
    workspace = Workspace.at(tmp_path / "ws")
    workspace.glossary_path.parent.mkdir(parents=True)
    workspace.glossary_path.write_text("stale\n", encoding="utf-8")

    write_snapshot(workspace, build_snapshot([_term("Fresh")]))

    assert workspace.glossary_path.read_text(encoding="utf-8") == "Fresh\n"
    assert not workspace.glossary_path.with_name("glossary.txt.tmp").exists()
