"""The system-native backend seam drives with no OS API present.

A native backend (Apple ``SpeechTranscriber``, Windows
``Microsoft.Windows.AI.Speech``) is *not* a ``whisper-cli`` subprocess, so the
seam must let it declare a different runtime, a different provisioning step and
its own availability reason — while the ggml plugin probe stays off it. These
tests use a fake system backend so none of that needs a Mac, a GPU or an OS
framework (ADR-0019).
"""

from __future__ import annotations

import pytest

import clear_record.providers.backends as backends
from clear_record.core.i18n import deferred
from clear_record.core.message import Message
from clear_record.providers import (
    APPLE_SPEECH_BACKEND_ID,
    RUNTIME_SYSTEM,
    RUNTIME_WHISPER_CLI,
    WINDOWS_AI_BACKEND_ID,
    Availability,
    BackendBase,
    BackendInfo,
    backend_availability,
    get_backend,
    probe_ggml_plugin_load,
)
from clear_record.providers.backends import BACKENDS


class _FakeSystemBackend(BackendBase):
    """A native backend that never touches an OS API: only the seam."""

    def __init__(self, *, available: bool, reason: Message | None = None) -> None:
        self.info = BackendInfo(
            id=APPLE_SPEECH_BACKEND_ID,
            vendor="Apple",
            frameworks=("Speech",),
            description="fake system backend",
            default_model="system",
            parallelizable=False,
            runtime=RUNTIME_SYSTEM,
            chunked=False,
        )
        self._available = available
        self._reason = reason
        self.prepared: list[tuple[str | None, str | None]] = []

    def availability(self) -> Availability:
        return Availability(self._available, self._reason)

    def prepare(self, model: str | None, model_dir: str | None) -> None:
        self.prepared.append((model, model_dir))
        return None

    def transcribe(self, audio_path: str, **kwargs):  # pragma: no cover - unused
        raise AssertionError("transcribe is not exercised by the seam test")


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #
def test_native_ids_are_documented_and_do_not_collide() -> None:
    assert APPLE_SPEECH_BACKEND_ID == "apple-speech"
    assert WINDOWS_AI_BACKEND_ID == "windows-ai"
    # The shipped ids are all whisper-cli; the native family keeps distinct ids
    # so a native backend registers without ambiguity (ADR-0019). Apple's adapter
    # has landed (ticket 02); Windows is still deferred (ticket 03), so its id is
    # documented but not yet a catalog entry.
    assert APPLE_SPEECH_BACKEND_ID in BACKENDS
    assert WINDOWS_AI_BACKEND_ID not in BACKENDS
    assert APPLE_SPEECH_BACKEND_ID not in ("apple", "nvidia", "amd")
    # The native backend is appended after the shipped trio so the positional
    # default (``next(iter(BACKENDS))``) stays a backend that can run anywhere.
    assert tuple(BACKENDS) == ("apple", "nvidia", "amd", APPLE_SPEECH_BACKEND_ID)


# --------------------------------------------------------------------------- #
# capability declaration
# --------------------------------------------------------------------------- #
def test_system_backend_declares_it_is_not_whisper_cli() -> None:
    info = _FakeSystemBackend(available=True).info
    assert info.runtime == RUNTIME_SYSTEM
    assert info.uses_ggml_plugin is False
    assert info.chunked is False
    # `parallelizable` is a per-backend statement, not the CLI adapter's.
    assert info.parallelizable is False


def test_whisper_cli_backends_still_advertise_the_ggml_probe() -> None:
    for backend_id in ("apple", "nvidia", "amd"):
        info = get_backend(backend_id).info
        assert info.runtime == RUNTIME_WHISPER_CLI
        assert info.uses_ggml_plugin is True
        assert info.chunked is True


# --------------------------------------------------------------------------- #
# availability: boolean plus reason
# --------------------------------------------------------------------------- #
def test_system_backend_reports_its_own_unavailable_reason() -> None:
    backend = _FakeSystemBackend(
        available=False,
        reason=Message(deferred("requires macOS 26+ (this is 15.0)")),
    )
    assert backend.available() is False
    status = backend.availability()
    assert status.available is False
    assert "macOS 26+" in str(status.reason)


def test_system_backend_available_derives_from_availability() -> None:
    backend = _FakeSystemBackend(available=True)
    # `available()` is inherited from BackendBase and reads `availability()`.
    assert backend.available() is True
    assert backend.availability().reason is None


def test_backend_base_requires_one_of_the_probe_pair() -> None:
    class Neither(BackendBase):
        pass

    with pytest.raises(NotImplementedError):
        Neither().available()


def test_backend_availability_reports_reasons(monkeypatch) -> None:
    fake = _FakeSystemBackend(
        available=False, reason=Message(deferred("speech asset not installed"))
    )
    monkeypatch.setitem(backends.BACKENDS, APPLE_SPEECH_BACKEND_ID, fake)

    report = backend_availability()

    assert report[APPLE_SPEECH_BACKEND_ID].available is False
    assert str(report[APPLE_SPEECH_BACKEND_ID].reason) == "speech asset not installed"


def test_whisper_cli_availability_names_the_failing_check(monkeypatch) -> None:
    monkeypatch.setattr(backends.platform, "system", lambda: "Linux")

    status = get_backend("apple").availability()

    assert status.available is False
    assert "Darwin" in str(status.reason)


# --------------------------------------------------------------------------- #
# the ggml plugin probe does not apply
# --------------------------------------------------------------------------- #
def test_system_backend_is_not_ggml_probed(monkeypatch) -> None:
    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the ggml probe must not shell out for a system backend")

    monkeypatch.setattr(backends.subprocess, "run", boom)
    monkeypatch.setattr(
        backends, "_find_whisper_cli", lambda: pytest.fail("must not look for a CLI")
    )

    probe = probe_ggml_plugin_load(_FakeSystemBackend(available=True))

    assert probe.loaded is None
    assert "does not apply" in probe.detail


# --------------------------------------------------------------------------- #
# prepare() is the provisioning step
# --------------------------------------------------------------------------- #
def test_system_backend_provisions_through_prepare() -> None:
    backend = _FakeSystemBackend(available=True)

    assert backend.prepare("system", None) is None
    assert backend.prepared == [("system", None)]
