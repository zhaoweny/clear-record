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


def test_whispercpp_segment_time_scale_10ms_units() -> None:
    """pywhispercpp 1.5.x reports t0/t1 in 10 ms units (JFK: 11 s -> 1100)."""
    from types import SimpleNamespace

    from cr_providers.backends import _whispercpp_segments

    fake = [
        SimpleNamespace(t0=0, t1=1100, text="hello world", probability=0.8),
    ]
    segs = _whispercpp_segments(fake, source="apple", language="en", duration=11.0)
    assert segs[0].start == pytest.approx(0.0)
    assert segs[0].end == pytest.approx(11.0, abs=0.05)
    assert segs[0].confidence == pytest.approx(0.8)


def test_whispercpp_segment_time_scale_ms_units() -> None:
    """Older bindings that report milliseconds are still handled (duration cue)."""
    from types import SimpleNamespace

    from cr_providers.backends import _whispercpp_segments

    fake = [
        SimpleNamespace(t0=0, t1=8000, text="a longer segment", probability=0.5),
    ]
    segs = _whispercpp_segments(fake, source="apple", language="en", duration=8.0)
    assert segs[0].end == pytest.approx(8.0, abs=0.05)
