"""Smoke tests for cr-providers: the backend catalog is declared without
importing any GPU framework."""

from __future__ import annotations

import pytest

from cr_providers import BackendInfo, available_backend_ids, get_backend
from cr_providers.backends import BACKENDS


def test_catalog_declares_three_families() -> None:
    assert tuple(BACKENDS) == ("apple", "nvidia", "amd")


def test_backend_metadata() -> None:
    apple = get_backend("apple")
    assert isinstance(apple.info, BackendInfo)
    assert apple.info.vendor == "Apple"
    assert "Metal" in apple.info.frameworks
    # Guard against silent overclaiming: the implemented Apple path is Metal
    # (`whisper-cli` + `ggml-metal`) and does not select Core ML or ANE.
    assert "Core ML" not in apple.info.frameworks
    assert "ANE" not in apple.info.frameworks
    nvidia = get_backend("nvidia")
    assert "CUDA" in nvidia.info.frameworks
    amd = get_backend("amd")
    assert amd.info.id == "amd"


def test_available_is_a_subset_and_never_imports_a_framework() -> None:
    ids = available_backend_ids()
    assert set(ids) <= {"apple", "nvidia", "amd"}


def test_get_backend_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_backend("does-not-exist")
