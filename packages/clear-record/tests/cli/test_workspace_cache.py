"""The chunk cache is app-owned cache, scoped per workspace (ADR-0007/ADR-0025)."""

from __future__ import annotations

from clear_record.cli.workspace import Workspace
from clear_record.core import paths


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
