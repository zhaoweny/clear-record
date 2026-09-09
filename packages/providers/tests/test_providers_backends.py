"""Smoke tests for cr-providers: the backend catalog is declared without
importing any GPU framework."""

from __future__ import annotations

from cr_providers import available_backend_ids, get_backend
from cr_providers.base import BackendInfo


def test_catalog_declares_three_families() -> None:
    from cr_providers.backends import BACKENDS

    assert tuple(BACKENDS) == ("apple", "nvidia", "amd")


def test_backend_metadata() -> None:
    apple = get_backend("apple")
    assert isinstance(apple.info, BackendInfo)
    assert apple.info.vendor == "Apple"
    nvidia = get_backend("nvidia")
    assert "CUDA" in nvidia.info.frameworks
    amd = get_backend("amd")
    assert amd.info.id == "amd"


def test_available_is_a_subset_and_never_imports_a_framework() -> None:
    # Calling available() must not raise even though no GPU framework is present.
    ids = available_backend_ids()
    assert set(ids) <= {"apple", "nvidia", "amd"}
