"""Smoke tests for clear_record.providers: the backend catalog is declared without
importing any GPU framework."""

from __future__ import annotations

import pytest

from clear_record.core.i18n import deferred
from clear_record.core.message import Message
from clear_record.providers import (
    APPLE_SPEECH_BACKEND_ID,
    Availability,
    BackendBase,
    BackendInfo,
    available_backend_ids,
    get_backend,
)
from clear_record.providers.backends import BACKENDS


class _StubSystemBackend(BackendBase):
    """An OS-free stand-in for ``apple-speech`` so this smoke test never probes
    the real Swift framework (which would compile a helper on a dev Mac)."""

    def __init__(self, available: bool) -> None:
        self.info = BackendInfo(
            id=APPLE_SPEECH_BACKEND_ID,
            vendor="Apple",
            frameworks=("Speech",),
            description="stub system backend",
            runtime="system",
            chunked=False,
        )
        self._available = available

    def availability(self) -> Availability:
        reason = None if self._available else Message(deferred("stubbed"))
        return Availability(self._available, reason)


def test_catalog_declares_the_whisper_cli_trio_plus_the_native_backend() -> None:
    # The native backend is appended after the shipped trio so the positional
    # default stays a portable backend (ADR-0019).
    assert tuple(BACKENDS) == (
        "apple",
        "nvidia",
        "amd",
        APPLE_SPEECH_BACKEND_ID,
    )


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


def test_available_is_a_subset_and_never_imports_a_framework(monkeypatch) -> None:
    # Stub the native backend so the smoke test does not compile/probe the real
    # Swift framework; this test is about the whisper-cli trio's probe shape.
    monkeypatch.setitem(BACKENDS, APPLE_SPEECH_BACKEND_ID, _StubSystemBackend(False))
    ids = available_backend_ids()
    assert set(ids) <= {"apple", "nvidia", "amd"}
    assert APPLE_SPEECH_BACKEND_ID not in ids


def test_get_backend_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_backend("does-not-exist")


def test_backend_base_prepare_is_a_no_op() -> None:
    """A backend with no downloadable model inherits the no-op default."""
    assert BackendBase().prepare("small", "/tmp/models") is None


def test_whisper_cli_prepare_resolves_a_local_model(tmp_path) -> None:
    """The whisper-cli adapter owns model resolution through the seam."""
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"stub")
    assert get_backend("apple").prepare("small", str(tmp_path)) == str(model)
