"""Tests for the system ``whisper-cli`` adapter (no GPU required).

The pure parsing/resolution helpers are exercised directly; ``transcribe`` is
driven against a *mocked* ``subprocess.run`` so the defensive JSON handling is
covered without any real GPU or ``whisper-cli``. The live probe stays
machine-dependent and is covered by the hot-test.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error

import pytest

import cr_providers.backends as backends
from cr_providers.backends import (
    AmdBackend,
    _GGML_BACKEND_DIRS,
    _resolve_ggml_model,
    _whispercli_segments,
)


def test_whispercli_segments_offsets_and_token_confidence() -> None:
    entries = [
        {
            "offsets": {"from": 0, "to": 5000},
            "text": "  hello world ",
            "tokens": [
                {"text": "[_BEG_]", "p": 0.10},
                {"text": " hello", "p": 0.80},
                {"text": " world", "p": 0.60},
            ],
        }
    ]
    segs = _whispercli_segments(entries, source="amd", language="en")
    assert len(segs) == 1
    assert segs[0].start == 0.0
    assert segs[0].end == 5.0
    assert segs[0].text == "hello world"
    assert segs[0].source == "amd"
    # Control token [_BEG_] is excluded: mean(0.8, 0.6) == 0.7.
    assert segs[0].confidence == pytest.approx(0.7)


def test_whispercli_segments_skips_empty_and_normalizes_reversed() -> None:
    entries = [
        {"offsets": {"from": 1000, "to": 2000}, "text": "   ", "tokens": []},
        {"offsets": {"from": 9000, "to": 4000}, "text": "back", "tokens": []},
    ]
    segs = _whispercli_segments(entries, source="amd", language="zh")
    assert len(segs) == 1
    assert segs[0].start == 4.0
    assert segs[0].end == 9.0
    assert segs[0].confidence is None  # no usable token probabilities


def test_resolve_ggml_model_by_name_and_path(tmp_path) -> None:
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"stub")
    assert _resolve_ggml_model("small", str(tmp_path)) == str(model)
    assert _resolve_ggml_model("ggml-small.bin", str(tmp_path)) == str(model)
    assert _resolve_ggml_model(str(model), None) == str(model)


# --------------------------------------------------------------------------- #
# First-run model download (no real network)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """A minimal ``urlopen`` result yielding ``chunks`` then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_urlopen(chunks: list[bytes], *, calls=None):
    def urlopen(url: str, timeout=None) -> _FakeResponse:
        if calls is not None:
            calls.append((url, timeout))
        return _FakeResponse(list(chunks))

    return urlopen


def test_resolve_ggml_model_downloads_when_absent(
    tmp_path, monkeypatch, capsys
) -> None:
    payload = b"ggml-model-bytes"
    calls: list[tuple[str, float | None]] = []
    monkeypatch.setattr(
        backends.urllib.request,
        "urlopen",
        _fake_urlopen([payload[:5], payload[5:]], calls=calls),
    )

    path = _resolve_ggml_model("medium", str(tmp_path))

    assert path == str(tmp_path / "ggml-medium.bin")
    assert (tmp_path / "ggml-medium.bin").read_bytes() == payload
    # The unique staging temp must not survive a successful download.
    assert [p.name for p in tmp_path.iterdir()] == ["ggml-medium.bin"]
    # A stalled connection must not block a worker indefinitely.
    assert calls and calls[0][1] == backends._GGML_DOWNLOAD_TIMEOUT_S
    err = capsys.readouterr().err
    assert "downloaded ggml-medium.bin" in err
    assert path in err


def test_resolve_ggml_model_respects_cr_models_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"tiny-bytes"])
    )

    path = _resolve_ggml_model("tiny", None)

    assert path == str(tmp_path / "ggml-tiny.bin")
    assert (tmp_path / "ggml-tiny.bin").read_bytes() == b"tiny-bytes"


def test_resolve_ggml_model_download_failure_raises_clear_error(
    tmp_path, monkeypatch
) -> None:
    def urlopen(url: str, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(backends.urllib.request, "urlopen", urlopen)

    with pytest.raises(FileNotFoundError, match=r"hf download"):
        _resolve_ggml_model("medium", str(tmp_path))

    # No model and no stray temp survives the failure.
    assert [p.name for p in tmp_path.iterdir()] == []


def test_resolve_ggml_model_interrupted_download_leaves_no_model(
    tmp_path, monkeypatch
) -> None:
    """A mid-stream failure rolls back the ``.part`` file, not a model."""

    class _Interrupted:
        def __init__(self) -> None:
            self._first = True

        def read(self, size: int = -1) -> bytes:
            if self._first:
                self._first = False
                return b"partial"
            raise OSError("connection reset")

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(
        backends.urllib.request, "urlopen", lambda url, timeout=None: _Interrupted()
    )

    with pytest.raises(FileNotFoundError, match=r"hf download"):
        _resolve_ggml_model("medium", str(tmp_path))

    assert [p.name for p in tmp_path.iterdir()] == []


def test_resolve_ggml_model_keyboard_interrupt_cleans_temp(
    tmp_path, monkeypatch
) -> None:
    """Ctrl-C mid-download must clean the temp and propagate it, not hide it
    behind a ``FileNotFoundError``."""

    def urlopen(url: str, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(backends.urllib.request, "urlopen", urlopen)

    with pytest.raises(KeyboardInterrupt):
        _resolve_ggml_model("medium", str(tmp_path))

    assert [p.name for p in tmp_path.iterdir()] == []


def test_resolve_ggml_model_is_single_flight_under_concurrency(
    tmp_path, monkeypatch
) -> None:
    """Concurrent first-use callers download exactly once, intact.

    Regression for the pool race: ``apple`` is parallelizable, so with no model
    present every worker used to stream into the shared ``.part`` and race
    ``os.replace`` (the loser raised a bare ``FileNotFoundError``, and the shared
    temp could be left corrupt). The lock plus a unique temp make the
    check-and-download single-flight.
    """
    payload = b"full-model-payload" * 64
    calls: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def urlopen(url: str, timeout=None):
        calls.append(url)  # recorded before any replace can happen
        time.sleep(0.3)  # keep both callers inside the download window
        return _FakeResponse([payload])

    monkeypatch.setattr(backends.urllib.request, "urlopen", urlopen)

    def worker() -> None:
        start.wait()
        try:
            _resolve_ggml_model("medium", str(tmp_path))
        except BaseException as exc:  # recorded for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(calls) == 1, f"expected exactly one download, got {len(calls)}"
    assert (tmp_path / "ggml-medium.bin").read_bytes() == payload
    # Only the model remains: no `.part` / temp from either caller.
    assert [p.name for p in tmp_path.iterdir()] == ["ggml-medium.bin"]


def test_amd_backend_metadata_unchanged() -> None:
    backend = AmdBackend()
    assert backend.info.id == "amd"
    assert "Vulkan" in backend.info.frameworks


def test_all_three_families_share_the_cli_adapter() -> None:
    """All three GPU families use the same process-isolated whisper-cli path."""
    from cr_providers.backends import AppleBackend, NvidiaBackend, _WhisperCliBackend

    assert isinstance(AppleBackend(), _WhisperCliBackend)
    assert isinstance(AmdBackend(), _WhisperCliBackend)
    assert isinstance(NvidiaBackend(), _WhisperCliBackend)
    assert AppleBackend().info.parallelizable
    assert AmdBackend().info.parallelizable
    assert NvidiaBackend().info.parallelizable


def test_has_nvidia_device_accepts_native_and_wsl(monkeypatch) -> None:
    monkeypatch.setattr(backends.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    monkeypatch.setattr(backends.os.path, "exists", lambda path: False)
    assert not backends._has_nvidia_device()

    # WSL2 exposes CUDA through /dev/dxg, with no /dev/nvidia* nodes.
    monkeypatch.setattr(backends.os.path, "exists", lambda path: path == "/dev/dxg")
    assert backends._has_nvidia_device()


def test_find_whisper_cli_rejects_generic_whisper(monkeypatch) -> None:
    """Only the whisper.cpp CLI counts; a bare `whisper` (e.g. OpenAI's) must
    not satisfy the probe, since it lacks the `-ojf`/`-of` interface."""
    monkeypatch.delenv("CR_WHISPER_CLI", raising=False)
    # Isolate from the Homebrew bin-dir fallback so the test stays
    # hardware-independent (a dev Mac may have a real /opt/homebrew/bin).
    monkeypatch.setattr(backends, "_WHISPER_CLI_BIN_DIRS", ())
    monkeypatch.setattr(
        backends.shutil,
        "which",
        lambda name: "/usr/bin/whisper" if name == "whisper" else None,
    )
    assert backends._find_whisper_cli() is None


def test_ggml_backend_dirs_include_debian_multiarch() -> None:
    """Debian/Ubuntu multiarch layouts must not be false negatives."""
    assert "/usr/lib/x86_64-linux-gnu/ggml" in _GGML_BACKEND_DIRS
    assert "/usr/lib/aarch64-linux-gnu/ggml" in _GGML_BACKEND_DIRS


# --------------------------------------------------------------------------- #
# `transcribe()` against a mocked whisper-cli (hardware-independent)
# --------------------------------------------------------------------------- #
def _stub_model(tmp_path) -> str:
    """An existing ``ggml-*.bin`` so ``_resolve_ggml_model`` succeeds."""
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"stub")
    return str(model)


def _install_fake_run(monkeypatch, behavior) -> None:
    """Point the adapter at a fake CLI and let ``behavior(cmd)`` answer."""
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: "/usr/bin/whisper-cli")

    def fake_run(cmd, **kwargs):
        return behavior(cmd)

    monkeypatch.setattr(backends.subprocess, "run", fake_run)


def _writes_json(payload):
    """A fake CLI that writes ``payload`` to its ``-of`` JSON path, exit 0."""

    def behavior(cmd):
        out_prefix = cmd[cmd.index("-of") + 1]
        with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return behavior


def test_transcribe_happy_path_ojf(tmp_path, monkeypatch) -> None:
    payload = {
        "result": {"language": "en"},
        "transcription": [
            {
                "offsets": {"from": 0, "to": 5000},
                "text": "  hello world ",
                "tokens": [
                    {"text": "[_BEG_]", "p": 0.10},
                    {"text": " hello", "p": 0.80},
                    {"text": " world", "p": 0.60},
                ],
            }
        ],
    }
    _install_fake_run(monkeypatch, _writes_json(payload))

    result = AmdBackend().transcribe(
        str(tmp_path / "a.wav"), model=_stub_model(tmp_path)
    )

    assert result.backend == "amd"
    assert result.language == "en"
    assert len(result.segments) == 1
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 5.0
    assert result.segments[0].text == "hello world"
    assert result.segments[0].confidence == pytest.approx(0.7)


def test_transcribe_missing_json_is_runtime_error(tmp_path, monkeypatch) -> None:
    """whisper.cpp exits 0 on an unknown flag, so no ``-ojf`` file is written."""

    def behavior(cmd):
        return subprocess.CompletedProcess(cmd, 0, "usage: whisper-cli ...", "")

    _install_fake_run(monkeypatch, behavior)

    with pytest.raises(RuntimeError, match=r"whisper-cli \(amd\).*-ojf"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_transcribe_malformed_json_is_runtime_error(tmp_path, monkeypatch) -> None:
    def behavior(cmd):
        out_prefix = cmd[cmd.index("-of") + 1]
        with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _install_fake_run(monkeypatch, behavior)

    with pytest.raises(RuntimeError, match="unparseable JSON"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_transcribe_nonzero_exit_is_runtime_error(tmp_path, monkeypatch) -> None:
    def behavior(cmd):
        return subprocess.CompletedProcess(cmd, 2, "", "unknown argument: -ojf")

    _install_fake_run(monkeypatch, behavior)

    with pytest.raises(RuntimeError, match=r"whisper-cli \(amd\) failed \(exit 2\)"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_transcribe_non_object_json_is_runtime_error(tmp_path, monkeypatch) -> None:
    _install_fake_run(monkeypatch, _writes_json([1, 2, 3]))

    with pytest.raises(RuntimeError, match="expected an object"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_transcribe_transcription_not_a_list_is_runtime_error(
    tmp_path, monkeypatch
) -> None:
    payload = {"result": {"language": "en"}, "transcription": {"not": "a list"}}
    _install_fake_run(monkeypatch, _writes_json(payload))

    with pytest.raises(RuntimeError, match="must be a list"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_whispercli_segments_rejects_bad_shapes() -> None:
    with pytest.raises(RuntimeError, match="must be a list"):
        _whispercli_segments({"text": "x"}, source="amd", language="en")
    with pytest.raises(RuntimeError, match="non-object transcription entry"):
        _whispercli_segments(["not-an-object"], source="amd", language="en")
    with pytest.raises(RuntimeError, match="non-object token"):
        _whispercli_segments(
            [{"offsets": {"from": 0, "to": 1}, "text": "x", "tokens": ["nope"]}],
            source="amd",
            language="en",
        )


@pytest.mark.parametrize("bad_text", [123, 1.5, True, ["a"], {"a": "b"}])
def test_whispercli_segments_rejects_non_string_text(bad_text) -> None:
    """A non-string ``text`` must raise, not leak ``AttributeError`` from
    ``(text or "").strip()`` inside ``_make_segment``."""
    with pytest.raises(RuntimeError, match="non-string 'text'"):
        _whispercli_segments(
            [{"offsets": {"from": 0, "to": 1}, "text": bad_text, "tokens": []}],
            source="amd",
            language="en",
        )


def test_whispercli_segments_accepts_none_text() -> None:
    """``None`` (and whitespace) is still a valid empty segment, not an error."""
    assert (
        _whispercli_segments(
            [{"offsets": {"from": 0, "to": 1}, "text": None, "tokens": []}],
            source="amd",
            language="en",
        )
        == ()
    )


def test_transcribe_non_string_text_is_runtime_error(tmp_path, monkeypatch) -> None:
    payload = {
        "result": {"language": "en"},
        "transcription": [
            {"offsets": {"from": 0, "to": 1000}, "text": 123, "tokens": []}
        ],
    }
    _install_fake_run(monkeypatch, _writes_json(payload))

    with pytest.raises(RuntimeError, match="non-string 'text'"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))


def test_transcribe_invalid_utf8_is_runtime_error(tmp_path, monkeypatch) -> None:
    """Invalid UTF-8 must become the same clear RuntimeError, not a
    ``UnicodeDecodeError``."""

    def behavior(cmd):
        out_prefix = cmd[cmd.index("-of") + 1]
        with open(out_prefix + ".json", "wb") as fh:
            fh.write(b'{"transcription": "\xff\xfe"}')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _install_fake_run(monkeypatch, behavior)

    with pytest.raises(RuntimeError, match="unparseable JSON"):
        AmdBackend().transcribe(str(tmp_path / "a.wav"), model=_stub_model(tmp_path))
