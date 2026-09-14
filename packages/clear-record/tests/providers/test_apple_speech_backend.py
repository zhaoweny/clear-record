"""The Apple ``SpeechTranscriber`` backend, driven with the OS call faked.

The real backend compiles and runs a small Swift helper against the macOS 26
``Speech`` framework. These tests replace that OS call with a fake helper, so the
suite is green on any platform and never compiles Swift. One opt-in hot test at
the bottom exercises the real framework on macOS 26 hardware
(``CR_APPLE_SPEECH_HOT=1``).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess

import pytest

import clear_record.providers.apple_speech as apple_speech
from clear_record.core import Segment, TranscriptionResult
from clear_record.providers import (
    APPLE_SPEECH_BACKEND_ID,
    RUNTIME_SYSTEM,
    AppleSpeechBackend,
    AppleSpeechError,
    AppleSpeechHelper,
    AppleSpeechUnavailable,
    SpeechProbe,
    get_backend,
    probe_ggml_plugin_load,
)
from clear_record.providers.apple_speech import (
    HELPER_ENV,
    _glossary_terms,
    _segments_from_helper,
)


class _FakeHelper:
    """A stand-in for :class:`AppleSpeechHelper` that never touches the OS."""

    def __init__(
        self,
        *,
        probe: SpeechProbe | None = None,
        probe_error: Exception | None = None,
        prepare_error: Exception | None = None,
        transcribe: dict | None = None,
        transcribe_error: Exception | None = None,
    ) -> None:
        self._probe = probe if probe is not None else SpeechProbe(True)
        self._probe_error = probe_error
        self._prepare_error = prepare_error
        self._transcribe = transcribe
        self._transcribe_error = transcribe_error
        self.probe_calls = 0
        self.prepared: list[str | None] = []
        self.calls: list[tuple[str, str | None, tuple[str, ...]]] = []

    def probe(self) -> SpeechProbe:
        self.probe_calls += 1
        if self._probe_error is not None:
            raise self._probe_error
        return self._probe

    def prepare(self, language: str | None) -> dict:
        if self._prepare_error is not None:
            raise self._prepare_error
        self.prepared.append(language)
        return {"locale": "en_US", "installed": True, "reserved": True}

    def transcribe(
        self, audio_path: str, *, language: str | None, terms: tuple[str, ...]
    ) -> dict:
        if self._transcribe_error is not None:
            raise self._transcribe_error
        self.calls.append((audio_path, language, terms))
        return self._transcribe or {"language": "en", "segments": []}


def _macos_26(monkeypatch) -> None:
    monkeypatch.setattr(apple_speech.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apple_speech.platform, "mac_ver", lambda: ("26.0", (), ""))


# --------------------------------------------------------------------------- #
# capabilities / registration
# --------------------------------------------------------------------------- #
def test_registered_as_a_system_backend() -> None:
    backend = get_backend(APPLE_SPEECH_BACKEND_ID)
    assert isinstance(backend, AppleSpeechBackend)
    assert backend.info.id == APPLE_SPEECH_BACKEND_ID
    assert backend.info.runtime == RUNTIME_SYSTEM
    assert backend.info.uses_ggml_plugin is False
    assert backend.info.chunked is False
    assert backend.info.parallelizable is False
    assert backend.info.decoder_knobs == ()
    assert "Speech" in backend.info.frameworks


def test_ggml_plugin_probe_does_not_apply() -> None:
    probe = probe_ggml_plugin_load(get_backend(APPLE_SPEECH_BACKEND_ID))
    assert probe.loaded is None
    assert "does not apply" in probe.detail


# --------------------------------------------------------------------------- #
# availability: cheap, reasoned, never an ImportError
# --------------------------------------------------------------------------- #
def test_unavailable_off_macos_without_probing(monkeypatch) -> None:
    monkeypatch.setattr(apple_speech.platform, "system", lambda: "Linux")
    helper = _FakeHelper()
    status = AppleSpeechBackend(helper).availability()

    assert status.available is False
    assert "macOS 26+" in status.reason
    assert "Linux" in status.reason
    # The platform gate is in-process: the helper is never invoked off macOS.
    assert helper.probe_calls == 0


def test_unavailable_on_pre_26_macos(monkeypatch) -> None:
    monkeypatch.setattr(apple_speech.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apple_speech.platform, "mac_ver", lambda: ("15.4", (), ""))
    helper = _FakeHelper()

    status = AppleSpeechBackend(helper).availability()

    assert status.available is False
    assert "requires macOS 26+" in status.reason
    assert "15.4" in status.reason
    assert helper.probe_calls == 0


def test_unavailable_when_the_helper_cannot_be_built(monkeypatch) -> None:
    _macos_26(monkeypatch)
    helper = _FakeHelper(
        probe_error=AppleSpeechUnavailable(
            "install the Swift toolchain with `xcode-select --install`"
        )
    )
    backend = AppleSpeechBackend(helper)

    status = backend.availability()

    assert status.available is False
    assert "xcode-select --install" in status.reason
    assert backend.available() is False


def test_unavailable_when_speech_transcriber_says_so(monkeypatch) -> None:
    _macos_26(monkeypatch)
    backend = AppleSpeechBackend(_FakeHelper(probe=SpeechProbe(False)))

    status = backend.availability()

    assert status.available is False
    assert "SpeechTranscriber" in status.reason


def test_available_on_macos_26(monkeypatch) -> None:
    _macos_26(monkeypatch)
    backend = AppleSpeechBackend(_FakeHelper(probe=SpeechProbe(True)))

    status = backend.availability()

    assert status.available is True
    assert "SpeechTranscriber" in status.reason
    assert backend.available() is True


# --------------------------------------------------------------------------- #
# prepare: the provisioning step
# --------------------------------------------------------------------------- #
def test_prepare_provisions_the_locale_asset() -> None:
    helper = _FakeHelper()
    backend = AppleSpeechBackend(helper)

    assert backend.prepare(None, None) is None
    assert helper.prepared == [None]


# --------------------------------------------------------------------------- #
# transcribe: timed, source-attributed segments
# --------------------------------------------------------------------------- #
def test_transcribe_maps_timed_source_attributed_segments() -> None:
    helper = _FakeHelper(
        transcribe={
            "locale": "en_US",
            "language": "en",
            "audioDuration": 12.5,
            "segments": [
                {"start": 0.0, "end": 1.5, "text": " hello", "confidence": 0.9},
                {"start": 1.5, "end": 3.0, "text": "world", "confidence": None},
                {"start": 3.0, "end": 4.0, "text": "   ", "confidence": 0.5},
            ],
        }
    )
    backend = AppleSpeechBackend(helper)

    result = backend.transcribe("a.wav", language="en")

    assert isinstance(result, TranscriptionResult)
    assert result.backend == APPLE_SPEECH_BACKEND_ID
    assert result.source == APPLE_SPEECH_BACKEND_ID
    assert result.language == "en"
    assert result.model == "system"
    assert result.audio_duration == 12.5
    assert [s.text for s in result.segments] == ["hello", "world"]
    assert all(s.source == APPLE_SPEECH_BACKEND_ID for s in result.segments)
    assert all(isinstance(s, Segment) for s in result.segments)
    assert result.segments[0].confidence == 0.9
    # Apple reported none for the second segment: it stays None, never invented.
    assert result.segments[1].confidence is None


def test_transcribe_ignores_auto_language_and_passes_the_hint() -> None:
    helper = _FakeHelper(transcribe={"language": "zh", "segments": []})
    backend = AppleSpeechBackend(helper)

    backend.transcribe("a.wav", language="auto")
    backend.transcribe("b.wav", language="zh")

    assert helper.calls == [("a.wav", None, ()), ("b.wav", "zh", ())]


def test_transcribe_passes_glossary_terms_to_the_helper() -> None:
    helper = _FakeHelper()
    backend = AppleSpeechBackend(helper)

    backend.transcribe("a.wav", initial_prompt="Acme Corp, Zhào Wén")

    assert helper.calls == [("a.wav", None, ("Acme Corp", "Zhào Wén"))]


def test_transcribe_surfaces_an_actionable_language_error() -> None:
    helper = _FakeHelper(
        transcribe_error=AppleSpeechError(
            "the Apple speech helper failed (exit 1): "
            "error: locale xx-XX is not supported by SpeechTranscriber"
        )
    )
    backend = AppleSpeechBackend(helper)

    with pytest.raises(AppleSpeechError, match="not supported"):
        backend.transcribe("a.wav", language="xx-XX")


# --------------------------------------------------------------------------- #
# the helper's subprocess contract (still no real OS call)
# --------------------------------------------------------------------------- #
def _fake_run(writes: dict, *, returncode: int = 0, stderr: str = ""):
    """A ``subprocess.run`` stand-in that writes ``writes`` to ``--out``."""

    def run(cmd, *, capture_output, text, timeout):
        out = cmd[cmd.index("--out") + 1]
        if writes is not None:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(writes, fh)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run


def _cached_helper(tmp_path, monkeypatch, run):
    binary = tmp_path / "helper"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(HELPER_ENV, str(binary))
    return AppleSpeechHelper(run=run, cache_dir=tmp_path / "cache")


def test_helper_probe_parses_the_verdict(tmp_path, monkeypatch) -> None:
    helper = _cached_helper(
        tmp_path,
        monkeypatch,
        _fake_run({"isAvailable": True, "supportedLocales": ["en_US", "zh_CN"]}),
    )

    probe = helper.probe()

    assert probe.is_available is True
    assert probe.supported_locales == ("en_US", "zh_CN")


def test_helper_transcribe_builds_the_expected_command(tmp_path, monkeypatch) -> None:
    seen: list[list[str]] = []

    def run(cmd, *, capture_output, text, timeout):
        seen.append(cmd)
        with open(cmd[cmd.index("--out") + 1], "w", encoding="utf-8") as fh:
            json.dump({"language": "en", "segments": []}, fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    helper = _cached_helper(tmp_path, monkeypatch, run)

    helper.transcribe("a.wav", language="en", terms=("Acme", "Zhào"))

    assert seen
    cmd = seen[0]
    assert cmd[1:3] == ["transcribe", "--audio"]
    assert cmd[cmd.index("--locale") + 1] == "en"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--term"] == [
        "Acme",
        "Zhào",
    ]


def test_helper_prepare_streams_and_reads_the_result(tmp_path, monkeypatch) -> None:
    streamed: list[bool] = []

    def run(cmd, *, capture_output, text, timeout):
        streamed.append(capture_output)
        with open(cmd[cmd.index("--out") + 1], "w", encoding="utf-8") as fh:
            json.dump({"locale": "en_US", "installed": True, "reserved": True}, fh)
        return subprocess.CompletedProcess(cmd, 0, None, None)

    helper = _cached_helper(tmp_path, monkeypatch, run)

    result = helper.prepare(None)

    assert result["installed"] is True
    # prepare streams stderr so the download progress is visible live.
    assert streamed == [False]


def test_helper_failure_is_a_clear_error(tmp_path, monkeypatch) -> None:
    helper = _cached_helper(
        tmp_path,
        monkeypatch,
        _fake_run(None, returncode=1, stderr="boom: nope"),
    )

    with pytest.raises(AppleSpeechError, match="boom: nope"):
        helper.probe()


def test_helper_requires_the_swift_toolchain(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(HELPER_ENV, raising=False)
    monkeypatch.setattr(apple_speech, "_swift_compiler", lambda: None)
    helper = AppleSpeechHelper(cache_dir=tmp_path / "cache")

    with pytest.raises(AppleSpeechUnavailable, match="xcode-select --install"):
        helper.ensure_binary()


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_glossary_terms_skip_blanks_and_comments() -> None:
    assert _glossary_terms(None) == ()
    assert _glossary_terms("") == ()
    assert _glossary_terms("Acme, # note\nZhào") == ("Acme", "Zhào")


def test_segments_from_helper_rejects_a_malformed_payload() -> None:
    with pytest.raises(AppleSpeechError, match="non-list 'segments'"):
        _segments_from_helper({"nope": 1}, source="apple-speech", language="en")


def test_segments_from_helper_tolerates_a_non_numeric_timestamp() -> None:
    segments = _segments_from_helper(
        [
            {"start": "x", "end": 1.0, "text": "skip"},
            {"start": 1.0, "end": 2.0, "text": "keep", "confidence": "0.5"},
        ],
        source="apple-speech",
        language="en",
    )
    assert [s.text for s in segments] == ["keep"]
    assert segments[0].confidence == 0.5


# --------------------------------------------------------------------------- #
# the real hot test -- opt-in, macOS 26 hardware only
# --------------------------------------------------------------------------- #
_HOT = os.environ.get("CR_APPLE_SPEECH_HOT", "").strip() not in ("", "0", "false")


def _macos_major() -> int | None:
    version = platform.mac_ver()[0]
    try:
        return int(version.split(".")[0]) if version else None
    except ValueError:
        return None


@pytest.mark.skipif(
    not _HOT,
    reason="opt-in hot test; set CR_APPLE_SPEECH_HOT=1 on macOS 26 hardware",
)
@pytest.mark.skipif(
    platform.system() != "Darwin" or (_macos_major() or 0) < 26,
    reason="requires macOS 26+",
)
@pytest.mark.skipif(
    shutil.which("say") is None or shutil.which("afconvert") is None,
    reason="needs the macOS say/afconvert tools to synthesize speech",
)
def test_hot_real_transcription(tmp_path) -> None:
    """Real ``SpeechTranscriber`` end-to-end: prepare, then transcribe speech.

    Not part of the default suite: it compiles the Swift helper (needs the
    toolchain) and downloads the locale asset on first use.
    """
    backend = AppleSpeechBackend()
    availability = backend.availability()
    if not availability.available:  # pragma: no cover - hardware dependent
        pytest.skip(f"apple-speech unavailable here: {availability.reason}")

    aiff = tmp_path / "speech.aiff"
    wav = tmp_path / "speech.wav"
    speech = "Hello world. This is a clear record hot test of Apple speech."
    try:
        # A named voice is more reliable than the default, which can be unset.
        subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), speech], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        subprocess.run(["say", "-o", str(aiff), speech], check=True)
    subprocess.run(
        [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
            str(aiff),
            str(wav),
        ],
        check=True,
    )

    backend.prepare(None, None)
    result = backend.transcribe(str(wav), language="en")

    assert result.backend == APPLE_SPEECH_BACKEND_ID
    assert result.segments, "the on-device model returned no segments"
    assert all(s.end >= s.start for s in result.segments)
    assert all(s.text for s in result.segments)
    # Confidence is honest: a float where Apple reported one, else None.
    assert all(
        s.confidence is None or isinstance(s.confidence, float) for s in result.segments
    )
