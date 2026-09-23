"""The chunk cache is app-owned cache, scoped per workspace (ADR-0007/ADR-0025)."""

from __future__ import annotations

from clear_record.pipeline.workspace import (
    CHUNK_GLOSSARY,
    Workspace,
    cache_plan,
    chunk_cache_key,
    chunk_glossary,
    glossary_digest,
    plan_matches,
)
from clear_record.core import Segment, paths


def test_chunk_cache_lives_under_the_app_cache_dir(tmp_path) -> None:
    cache = Workspace.at(tmp_path / "w").chunk_cache("s1")
    assert cache.directory.is_relative_to(paths.resolve_cache_dir())
    assert cache.directory.name == "s1"


def test_two_workspaces_never_share_a_cache(tmp_path) -> None:
    first = Workspace.at(tmp_path / "a").chunk_cache("s1")
    second = Workspace.at(tmp_path / "b").chunk_cache("s1")
    assert first.directory != second.directory


def test_the_same_workspace_resumes_the_same_cache(tmp_path) -> None:
    first = Workspace.at(tmp_path / "a").chunk_cache("s1")
    again = Workspace.at(tmp_path / "a").chunk_cache("s1")
    assert first.directory == again.directory


# --- the split key: plan vs per-chunk glossary ------------------------------- #
def _key(glossary: str = "Acme") -> dict:
    return chunk_cache_key(
        backend="amd",
        model="small",
        language=None,
        glossary=glossary,
        chunk_seconds=600.0,
        overlap_seconds=5.0,
        n_chunks=3,
    )


def test_the_plan_is_the_key_without_the_glossary() -> None:
    assert cache_plan(_key()) == cache_plan(_key("Different"))
    assert "glossary" not in cache_plan(_key())
    assert cache_plan(_key())["n_chunks"] == 3


def test_a_plan_change_still_matches_nothing() -> None:
    """Backend/model/language/chunk plan/decoders still invalidate everything."""
    assert plan_matches(_key(), _key()) is True
    assert plan_matches(None, _key()) is False
    assert plan_matches(_key(), {**_key(), "n_chunks": 4}) is False
    assert plan_matches(_key(), {**_key(), "model": "large"}) is False


def test_per_chunk_glossary_digests_survive_a_round_trip(tmp_path) -> None:
    cache = Workspace.at(tmp_path / "w").chunk_cache("s1").ensure()
    cache.write_meta(_key(), {0: "aaaa", 1: "bbbb"})
    stored = cache.read_meta()
    assert chunk_glossary(stored) == {0: "aaaa", 1: "bbbb"}
    assert plan_matches(stored, _key()) is True
    # The digest of the run's own glossary is derivable, so a chunk that is
    # already current is recognizable.
    assert glossary_digest("Acme") == glossary_digest("Acme")
    assert glossary_digest("Acme") != glossary_digest("Acme Ltd")


def test_a_meta_without_per_chunk_provenance_reads_as_unknown(tmp_path) -> None:
    """A cache written before scoped re-runs must not be trusted (migration).

    ``chunk_glossary`` returning ``{}`` is what makes the transcriber treat every
    body as absent and re-decode it once, rather than reuse it at unknown
    provenance.
    """
    cache = Workspace.at(tmp_path / "w").chunk_cache("s1").ensure()
    cache.write_meta(_key())  # the old, glossary-only layout
    cache.write_segments(0, [Segment(0.0, 1.0, "hi", "s1")])
    stored = cache.read_meta()
    assert CHUNK_GLOSSARY not in stored
    assert chunk_glossary(stored) == {}
    assert plan_matches(stored, _key()) is True
