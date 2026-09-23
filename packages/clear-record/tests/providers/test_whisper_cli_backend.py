"""Tests for the system ``whisper-cli`` adapter (no GPU required).

The pure parsing/resolution helpers are exercised directly; ``transcribe`` is
driven against a *mocked* ``subprocess.run`` so the defensive JSON handling is
covered without any real GPU or ``whisper-cli``. The live probe stays
machine-dependent and is covered by the hot-test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error

import pytest

import clear_record.providers.backends as backends
from clear_record.core import DECODER_KNOBS, DECODER_KNOB_FIELDS
from clear_record.providers.backends import (
    AmdBackend,
    AppleBackend,
    NvidiaBackend,
    _GGML_BACKEND_DIRS,
    _resolve_ggml_model,
    _whispercli_segments,
)
from clear_record.providers.ggml_hashes import (
    ENV_MODEL_CHECKSUM,
    GGML_MODEL_SHA256,
    ModelChecksumError,
    checksum_enabled,
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
@pytest.fixture(autouse=True)
def _checksum_switch_is_hermetic(monkeypatch) -> None:
    """A developer's exported ``CR_MODEL_CHECKSUM`` must not change a test.

    The switch defaults to *on*, so clearing it here lets the download tests
    assert the real default; a test that wants it off sets it itself.
    """
    monkeypatch.delenv(ENV_MODEL_CHECKSUM, raising=False)


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


def _pin_digest(monkeypatch, name: str, payload: bytes) -> None:
    """Point ``name``'s pinned digest at ``payload``, in the one shipped table.

    The download tests exercise the mechanics with tiny fake models, so they
    override the real digest through the table's single home rather than
    restating a production hash — which could silently drift from what the
    download actually checks.
    """
    monkeypatch.setitem(GGML_MODEL_SHA256, name, hashlib.sha256(payload).hexdigest())


def test_resolve_ggml_model_downloads_when_absent(
    tmp_path, monkeypatch, capsys
) -> None:
    payload = b"ggml-model-bytes"
    calls: list[tuple[str, float | None]] = []
    _pin_digest(monkeypatch, "ggml-medium.bin", payload)
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
    assert "sha256 verified" in err
    assert path in err


def test_resolve_ggml_model_respects_cr_models_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path))
    _pin_digest(monkeypatch, "ggml-tiny.bin", b"tiny-bytes")
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"tiny-bytes"])
    )

    path = _resolve_ggml_model("tiny", None)

    assert path == str(tmp_path / "ggml-tiny.bin")
    assert (tmp_path / "ggml-tiny.bin").read_bytes() == b"tiny-bytes"


def test_resolve_ggml_model_honours_hf_endpoint(tmp_path, monkeypatch) -> None:
    """A reachable mirror served via ``HF_ENDPOINT`` replaces the pinned host."""
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
    calls: list[tuple[str, float | None]] = []
    _pin_digest(monkeypatch, "ggml-medium.bin", b"bytes")
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"bytes"], calls=calls)
    )

    _resolve_ggml_model("medium", str(tmp_path))

    assert len(calls) == 1
    url = calls[0][0]
    assert url.startswith("https://hf-mirror.com/")
    assert url.endswith("ggml-medium.bin")


def test_resolve_ggml_model_defaults_to_huggingface(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    calls: list[tuple[str, float | None]] = []
    _pin_digest(monkeypatch, "ggml-medium.bin", b"bytes")
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"bytes"], calls=calls)
    )

    _resolve_ggml_model("medium", str(tmp_path))

    assert len(calls) == 1
    url = calls[0][0]
    assert url.startswith("https://huggingface.co/")
    assert url.endswith("ggml-medium.bin")


def test_resolve_ggml_model_strips_trailing_slash_from_endpoint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com/")
    calls: list[tuple[str, float | None]] = []
    _pin_digest(monkeypatch, "ggml-medium.bin", b"bytes")
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"bytes"], calls=calls)
    )

    _resolve_ggml_model("medium", str(tmp_path))

    assert len(calls) == 1
    url = calls[0][0]
    assert (
        url
        == "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin"
    )
    assert "com//" not in url


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
    _pin_digest(monkeypatch, "ggml-medium.bin", payload)

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


# --------------------------------------------------------------------------- #
# Download integrity: the pinned SHA-256 check (defence in depth)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, enabled",
    [
        (None, True),  # unset -> the check stays on
        ("", True),  # blank -> on, i.e. fail closed
        ("off", False),
        ("OFF", False),
        (" off ", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("on", True),
        ("strict", True),
        ("ofl", True),  # a typo must not silently disable the check
    ],
)
def test_checksum_switch_only_disables_on_an_explicit_falsy_value(value, enabled):
    environ = {} if value is None else {ENV_MODEL_CHECKSUM: value}
    assert checksum_enabled(environ) is enabled


def test_checksum_mismatch_discards_the_download_and_raises(tmp_path, monkeypatch):
    """A complete body of the wrong bytes must never be installed as a model."""
    name = "ggml-medium.bin"
    _pin_digest(monkeypatch, name, b"the-expected-bytes")
    monkeypatch.setattr(
        backends.urllib.request, "urlopen", _fake_urlopen([b"substituted-bytes"])
    )

    with pytest.raises(ModelChecksumError) as excinfo:
        _resolve_ggml_model("medium", str(tmp_path))

    exc = excinfo.value
    assert "SHA-256" in str(exc)
    assert exc.params["name"] == name
    assert exc.params["expected"] == GGML_MODEL_SHA256[name]
    assert ENV_MODEL_CHECKSUM in str(exc)
    # A stable ID plus parameters: the English rendering is exactly what a
    # `tr(exc.msgid, **exc.params)` presentation boundary would reproduce.
    assert exc.msgid.format(**exc.params) == str(exc)
    # Fail closed: neither the model nor the staged `.part` survives.
    assert [p.name for p in tmp_path.iterdir()] == []


def test_checksum_can_be_disabled_for_a_mirror(tmp_path, monkeypatch, capsys):
    """``CR_MODEL_CHECKSUM=off`` accepts the endpoint's bytes unchecked."""
    payload = b"mirror-serves-its-own-bytes"
    _pin_digest(monkeypatch, "ggml-medium.bin", b"the-canonical-bytes")
    monkeypatch.setenv(ENV_MODEL_CHECKSUM, "off")
    monkeypatch.setattr(backends.urllib.request, "urlopen", _fake_urlopen([payload]))

    path = _resolve_ggml_model("medium", str(tmp_path))

    assert path == str(tmp_path / "ggml-medium.bin")
    assert (tmp_path / "ggml-medium.bin").read_bytes() == payload
    assert "sha256 verified" not in capsys.readouterr().err


def test_unpinned_model_downloads_unchecked(tmp_path, monkeypatch, capsys):
    """A name with no pinned digest has nothing to check, so it is not an error."""
    assert "ggml-medium-q5_0.bin" not in GGML_MODEL_SHA256
    payload = b"an-unpinned-quantization"
    monkeypatch.setattr(backends.urllib.request, "urlopen", _fake_urlopen([payload]))

    path = _resolve_ggml_model("medium-q5_0", str(tmp_path))

    assert path == str(tmp_path / "ggml-medium-q5_0.bin")
    assert (tmp_path / "ggml-medium-q5_0.bin").read_bytes() == payload
    assert "sha256 verified" not in capsys.readouterr().err


def test_present_model_needs_no_network_and_is_not_re_hashed(tmp_path, monkeypatch):
    """Offline/manual pre-fetch is unchanged: a model on disk is returned as-is.

    Verification costs a full read of a GB-scale file, so it belongs to the
    download that already streams those bytes -- not to every later resolve.
    """
    model = tmp_path / "ggml-medium.bin"
    model.write_bytes(b"pre-fetched by hand")

    def urlopen(url: str, timeout=None):
        raise AssertionError("a model already on disk must not be re-fetched")

    monkeypatch.setattr(backends.urllib.request, "urlopen", urlopen)

    assert _resolve_ggml_model("medium", str(tmp_path)) == str(model)
    assert model.read_bytes() == b"pre-fetched by hand"


def test_pinned_table_covers_the_auto_ladder() -> None:
    """Every size ``--auto`` can recommend has a digest to check.

    The ladder lives in ``pipeline.auto``; reading it here keeps the two in step
    without restating the list in a second place.
    """
    from clear_record.pipeline.auto import MODEL_LADDER

    missing = [
        size for size in MODEL_LADDER if f"ggml-{size}.bin" not in GGML_MODEL_SHA256
    ]
    assert not missing, f"no pinned digest for auto ladder size(s): {missing}"


def test_pinned_digests_are_well_formed() -> None:
    assert GGML_MODEL_SHA256
    for name, digest in GGML_MODEL_SHA256.items():
        assert name.startswith("ggml-") and name.endswith(".bin")
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{name}: {digest!r}"


def test_amd_backend_metadata_unchanged() -> None:
    backend = AmdBackend()
    assert backend.info.id == "amd"
    assert "Vulkan" in backend.info.frameworks


def test_all_three_families_share_the_cli_adapter() -> None:
    """All three GPU families use the same process-isolated whisper-cli path."""
    from clear_record.providers.backends import (
        AppleBackend,
        NvidiaBackend,
        _WhisperCliBackend,
    )

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


def test_ggml_backend_dirs_cover_real_distro_layouts() -> None:
    """Pin the verified plugin locations so a real non-Arch install is not a
    false negative (see the comment on ``_GGML_BACKEND_DIRS``)."""
    for path in (
        "/usr/lib/ggml",  # Arch ggml-*
        "/usr/lib/x86_64-linux-gnu/ggml",  # Ubuntu 25.10 / Debian
        "/usr/lib/x86_64-linux-gnu/ggml/backends*",  # Ubuntu 26.04, ggml >= 0.9
        "/usr/lib64",  # Fedora whisper-cpp (direct)
        "/usr/lib",  # upstream `cmake --install` prefix=/usr (direct)
        "/nix/store/*whisper-cpp*/lib",  # Nix
    ):
        assert path in _GGML_BACKEND_DIRS
    assert "/usr/lib/aarch64-linux-gnu/ggml/backends*" in _GGML_BACKEND_DIRS
    # A ggml >= 0.9 nested build can appear under *any* base dir, not only the
    # multiarch tuple.
    for nested in (
        "/usr/lib/ggml/backends*",
        "/usr/lib64/ggml/backends*",
        "/usr/lib/x86_64-linux-gnu/backends*",
        "/usr/lib/backends*",
        "/usr/lib64/backends*",
        "/nix/store/*whisper-cpp*/lib/backends*",
    ):
        assert nested in _GGML_BACKEND_DIRS


def test_ggml_backend_dirs_override_is_prepended(monkeypatch) -> None:
    """``CR_GGML_BACKEND_DIRS`` wins: it is searched before the built-in list."""
    monkeypatch.setenv("CR_GGML_BACKEND_DIRS", f"/opt/one{os.pathsep}/opt/two")
    dirs = backends._ggml_backend_dirs()
    assert dirs[:2] == ("/opt/one", "/opt/two")
    assert dirs[2:] == _GGML_BACKEND_DIRS


def test_find_ggml_gpu_backend_finds_backends0_layout(tmp_path, monkeypatch) -> None:
    """A plugin under Debian/Ubuntu's ``ggml/backends0/`` must be found."""
    d = tmp_path / "ggml" / "backends0"
    d.mkdir(parents=True)
    plugin = d / "libggml-vulkan.so"
    plugin.write_bytes(b"stub")
    monkeypatch.setenv("CR_GGML_BACKEND_DIRS", str(d))
    assert backends._find_ggml_gpu_backend(("vulkan",)) == str(plugin)


def test_find_ggml_gpu_backend_expands_nested_backends_glob(
    tmp_path, monkeypatch
) -> None:
    """The static ``backends*`` globs find a nested (future ``backendsN``) dir."""
    d = tmp_path / "ggml" / "backends1"
    d.mkdir(parents=True)
    plugin = d / "libggml-vulkan.so"
    plugin.write_bytes(b"stub")
    monkeypatch.delenv("CR_GGML_BACKEND_DIRS", raising=False)
    monkeypatch.setattr(
        backends, "_GGML_BACKEND_DIRS", (str(tmp_path / "ggml" / "backends*"),)
    )
    assert backends._find_ggml_gpu_backend(("vulkan",)) == str(plugin)


# --------------------------------------------------------------------------- #
# Opt-in plugin-load probe (`--check-plugin`) -- mocked whisper-cli
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_plugin_probe_cache():
    backends._clear_plugin_load_cache()
    yield
    backends._clear_plugin_load_cache()


def _probe_with_output(monkeypatch, output: str, *, calls=None):
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: "/usr/bin/whisper-cli")

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", output)

    monkeypatch.setattr(backends.subprocess, "run", fake_run)


def test_probe_ggml_plugin_load_confirms_matching_backend(monkeypatch) -> None:
    _probe_with_output(
        monkeypatch,
        "load_backend: loaded Vulkan backend from /usr/lib/ggml/libggml-vulkan.so\n",
    )
    probe = backends.probe_ggml_plugin_load(AmdBackend())
    assert probe.loaded is True
    assert "vulkan" in probe.detail.lower()


def test_probe_ggml_plugin_load_flags_a_different_backend(monkeypatch) -> None:
    _probe_with_output(
        monkeypatch, "load_backend: loaded CUDA backend from /usr/lib/libggml-cuda.so\n"
    )
    probe = backends.probe_ggml_plugin_load(AmdBackend())
    assert probe.loaded is False
    assert "cuda" in probe.detail.lower()


def test_probe_ggml_plugin_load_accepts_release_device_banner(monkeypatch) -> None:
    """Release builds suppress ``load_backend``; a device banner still counts."""
    _probe_with_output(monkeypatch, "ggml_vulkan: No devices found.\n")
    assert backends.probe_ggml_plugin_load(AmdBackend()).loaded is True


def test_probe_ggml_plugin_load_rejects_negative_context(monkeypatch) -> None:
    """A family named only in a failure must not be read as loaded.

    Regression: a bare substring match used to classify ``error: failed to load
    vulkan backend`` as ``loaded=True`` -- a false OK from the probe whose job is
    to catch exactly that.
    """
    _probe_with_output(monkeypatch, "error: failed to load vulkan backend\n")
    probe = backends.probe_ggml_plugin_load(AmdBackend())
    assert probe.loaded is False
    assert "failure" in probe.detail.lower()


def test_probe_ggml_plugin_load_rejects_no_backend_line(monkeypatch) -> None:
    _probe_with_output(monkeypatch, "warning: no vulkan backend available\n")
    assert backends.probe_ggml_plugin_load(AmdBackend()).loaded is False


def test_probe_ggml_plugin_load_accepts_positive_after_a_negative_line(
    monkeypatch,
) -> None:
    """A later positive banner still wins over an earlier failure line."""
    _probe_with_output(
        monkeypatch,
        "error: failed to load vulkan backend\nggml_vulkan: Found 1 Vulkan devices:\n",
    )
    assert backends.probe_ggml_plugin_load(AmdBackend()).loaded is True


def test_probe_ggml_plugin_load_inconclusive_when_output_is_silent(monkeypatch) -> None:
    _probe_with_output(monkeypatch, "whisper.cpp version: 1.9.3\n")
    probe = backends.probe_ggml_plugin_load(AmdBackend())
    assert probe.loaded is None


def test_probe_ggml_plugin_load_is_cached_per_invocation(monkeypatch) -> None:
    calls: list = []
    _probe_with_output(
        monkeypatch,
        "load_backend: loaded Vulkan backend from /usr/lib/ggml/libggml-vulkan.so\n",
        calls=calls,
    )
    backend = AmdBackend()
    assert backends.probe_ggml_plugin_load(backend).loaded is True
    assert backends.probe_ggml_plugin_load(backend).loaded is True
    assert len(calls) == 1, "the probe must run at most once per CLI invocation"


def test_probe_ggml_plugin_load_without_cli(monkeypatch) -> None:
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: None)
    probe = backends.probe_ggml_plugin_load(AmdBackend())
    assert probe.loaded is False
    assert "not found" in probe.detail


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


# --------------------------------------------------------------------------- #
# Decoder knobs on the built command (unset leaves it unchanged)
# --------------------------------------------------------------------------- #
def _capture_command(monkeypatch, captured: list[list[str]]):
    """A fake CLI that records the command and answers with an empty transcript."""

    def behavior(cmd):
        captured.append(list(cmd))
        out_prefix = cmd[cmd.index("-of") + 1]
        with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
            json.dump({"result": {"language": "en"}, "transcription": []}, fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _install_fake_run(monkeypatch, behavior)


def test_transcribe_without_knobs_leaves_the_command_unchanged(
    tmp_path, monkeypatch
) -> None:
    """The byte-identical promise: no decoder flags appear when none are set."""
    captured: list[list[str]] = []
    _capture_command(monkeypatch, captured)
    model = _stub_model(tmp_path)

    AmdBackend().transcribe(str(tmp_path / "a.wav"), model=model)

    (cmd,) = captured
    assert cmd == [
        "/usr/bin/whisper-cli",
        "-m",
        model,
        "-f",
        str(tmp_path / "a.wav"),
        "-l",
        "auto",
        "-ojf",
        "-of",
        cmd[cmd.index("-of") + 1],
    ]


def test_transcribe_builds_each_decoder_flag_when_set(tmp_path, monkeypatch) -> None:
    """Every decoder knob the declaration names reaches the command as its own
    declared flag, with the value it was given. The flag and the sample value are
    read off the declaration, so this states no second copy of the knob list."""
    captured: list[list[str]] = []
    _capture_command(monkeypatch, captured)

    values = {
        knob.name: knob.convert("4") if knob.convert is int else knob.convert("0.5")
        for knob in DECODER_KNOBS
    }
    AmdBackend().transcribe(
        str(tmp_path / "a.wav"), model=_stub_model(tmp_path), **values
    )

    (cmd,) = captured
    for knob in DECODER_KNOBS:
        assert knob.provider_flag in cmd, f"{knob.provider_flag} missing from {cmd}"
        assert cmd[cmd.index(knob.provider_flag) + 1] == str(values[knob.name])


def test_every_decoder_knob_declares_a_distinct_whisper_cli_flag() -> None:
    """The declaration is where the knob/flag pairing lives; `_decoder_flags`
    builds the command's map from these rows and raises if one has no flag, so
    this states the invariant that build depends on: one flag per knob."""
    flags = [knob.provider_flag for knob in DECODER_KNOBS]

    assert all(flags), "a decoder knob declares no whisper-cli flag"
    assert len(set(flags)) == len(flags), f"two knobs share a flag: {flags}"


def test_transcribe_rejects_a_keyword_that_is_not_a_declared_knob() -> None:
    """The adapter takes the knobs as keywords rather than parameters, so it must
    refuse a name the declaration does not have: a typo has to be an error, not
    an argument quietly dropped on the floor."""
    with pytest.raises(TypeError, match="beam_sizee"):
        AmdBackend().transcribe("a.wav", beam_sizee=5)


def test_all_whisper_cli_backends_advertise_the_decoder_knobs() -> None:
    for backend in (AppleBackend(), AmdBackend(), NvidiaBackend()):
        assert backend.info.decoder_knobs == DECODER_KNOB_FIELDS
