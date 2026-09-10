"""Hardware-independent tests for the Apple backend probe and routing.

The live Metal probe is machine-dependent (a real Mac + Homebrew ``whisper-cpp``
+ ggml plugin). Here we fake Darwin, a ``CR_WHISPER_CLI`` path and a
``libggml-metal.so`` under a tmp dir, so the prefer-CLI / in-process-fallback
routing is exercised with no Mac, no Homebrew and no GPU.

The Metal plugin on this install is `libggml-metal.so` under
`.../ggml/<version>/libexec/` (Homebrew) — note the ``.so`` extension on macOS.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import cr_providers.backends as backends
from cr_providers.backends import (
    AppleBackend,
    _GGML_BACKEND_DIRS,
    _WhisperCliBackend,
    _WhisperCppBackend,
    _find_ggml_gpu_backend,
)


def _fake_darwin(monkeypatch) -> None:
    monkeypatch.setattr(backends.platform, "system", lambda: "Darwin")


def _fake_cli_and_metal(tmp_path, monkeypatch) -> None:
    """A fake CLI file + a fake ``libggml-metal.so`` reachable via env overrides."""
    cli = tmp_path / "bin" / "whisper-cli"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("CR_WHISPER_CLI", str(cli))
    libdir = tmp_path / "ggml"
    libdir.mkdir()
    (libdir / "libggml-metal.so").write_bytes(b"stub")
    monkeypatch.setenv("CR_GGML_BACKEND_DIRS", str(libdir))
    # Isolate from a real Homebrew install so the test exercises only the tmp
    # fake (hardware-independent even on a dev Mac).
    monkeypatch.setattr(backends, "_GGML_BACKEND_DIRS", ())


# --------------------------------------------------------------------------- #
# Probe: Darwin + whisper-cli + Metal plugin
# --------------------------------------------------------------------------- #
def test_apple_probe_available_on_darwin_with_cli_and_metal(tmp_path, monkeypatch):
    _fake_darwin(monkeypatch)
    _fake_cli_and_metal(tmp_path, monkeypatch)

    backend = AppleBackend()

    assert backend.info.id == "apple"
    assert backend.available()
    # Routing targets: the CLI adapter is preferred, the wheel is the fallback.
    assert isinstance(backend._cli, _WhisperCliBackend)
    assert isinstance(backend._inprocess, _WhisperCppBackend)


def test_apple_unavailable_off_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(backends.platform, "system", lambda: "Linux")
    _fake_cli_and_metal(tmp_path, monkeypatch)
    monkeypatch.setattr(backends, "_whispercpp_available", lambda: True)

    assert not AppleBackend().available()


def test_apple_unavailable_without_cli_or_wheel(monkeypatch):
    _fake_darwin(monkeypatch)
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: None)
    monkeypatch.setattr(backends, "_whispercpp_available", lambda: False)

    assert not AppleBackend().available()


def test_apple_probe_without_metal_plugin_uses_wheel(tmp_path, monkeypatch):
    """A pip-only Mac (CLI absent, wheel importable) is still available."""
    _fake_darwin(monkeypatch)
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: None)
    monkeypatch.setattr(backends, "_whispercpp_available", lambda: True)

    assert AppleBackend().available()


# --------------------------------------------------------------------------- #
# Routing: prefer whisper-cli, fall back to in-process
# --------------------------------------------------------------------------- #
def test_apple_transcribe_routes_to_whisper_cli(tmp_path, monkeypatch):
    _fake_darwin(monkeypatch)
    _fake_cli_and_metal(tmp_path, monkeypatch)
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"stub")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out_prefix = cmd[cmd.index("-of") + 1]
        payload = {
            "result": {"language": "en"},
            "transcription": [
                {
                    "offsets": {"from": 0, "to": 1000},
                    "text": " hi",
                    "tokens": [{"text": " hi", "p": 0.9}],
                }
            ],
        }
        with open(out_prefix + ".json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)

    def _boom(*args, **kwargs):
        raise AssertionError("in-process fallback must not run when the CLI exists")

    monkeypatch.setattr(_WhisperCppBackend, "transcribe", _boom)

    result = AppleBackend().transcribe(str(tmp_path / "a.wav"), model=str(model))

    assert calls, "the whisper-cli subprocess path must be used"
    assert result.backend == "apple"
    assert result.language == "en"
    assert result.segments[0].text == "hi"


def test_apple_transcribe_falls_back_to_inprocess_without_cli(monkeypatch):
    _fake_darwin(monkeypatch)
    monkeypatch.setattr(backends, "_find_whisper_cli", lambda: None)
    monkeypatch.setattr(backends, "_whispercpp_available", lambda: True)

    sentinel = object()

    def _inprocess(self, audio_path, **kwargs):
        return sentinel

    monkeypatch.setattr(_WhisperCppBackend, "transcribe", _inprocess)

    def _boom(*args, **kwargs):
        raise AssertionError("the CLI path must not run without a whisper-cli")

    monkeypatch.setattr(backends.subprocess, "run", _boom)

    backend = AppleBackend()
    assert backend.available()
    assert backend.transcribe("a.wav") is sentinel


# --------------------------------------------------------------------------- #
# Metal pattern / macOS dir detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "libggml-metal.so",
        "libggml-metal.0.so",
        "libggml-metal.dylib",
        "libggml-metal.0.dylib",
        "ggml-metal.metal",
    ],
)
def test_metal_backend_patterns_match(name, tmp_path, monkeypatch):
    (tmp_path / name).write_bytes(b"stub")
    monkeypatch.setenv("CR_GGML_BACKEND_DIRS", str(tmp_path))
    # Isolate from a real Homebrew install (e.g. this dev Mac), so only the
    # tmp plugin can match.
    monkeypatch.setattr(backends, "_GGML_BACKEND_DIRS", ())

    assert _find_ggml_gpu_backend(("metal",)) == str(tmp_path / name)


def test_ggml_backend_dirs_include_macos_layouts() -> None:
    # Homebrew puts the Metal plugin under libexec (the stable opt/ symlink and
    # the versioned Cellar), plus the plain lib dirs for other builds.
    assert "/opt/homebrew/lib" in _GGML_BACKEND_DIRS
    assert "/opt/homebrew/libexec" in _GGML_BACKEND_DIRS
    assert "/opt/homebrew/opt/ggml/libexec" in _GGML_BACKEND_DIRS
    assert "/usr/local/lib" in _GGML_BACKEND_DIRS
    assert "/usr/local/opt/ggml/libexec" in _GGML_BACKEND_DIRS
    assert any(
        d.startswith("/opt/homebrew/Cellar/ggml/") and d.endswith("libexec")
        for d in _GGML_BACKEND_DIRS
    )


def test_metal_plugin_found_under_cellar_libexec_glob(tmp_path, monkeypatch) -> None:
    """The real Homebrew layout is `<Cellar>/ggml/<ver>/libexec/libggml-metal.so`."""
    version_dir = tmp_path / "Cellar" / "ggml" / "0.23.0" / "libexec"
    version_dir.mkdir(parents=True)
    (version_dir / "libggml-metal.so").write_bytes(b"stub")

    monkeypatch.delenv("CR_GGML_BACKEND_DIRS", raising=False)
    monkeypatch.setattr(
        backends,
        "_GGML_BACKEND_DIRS",
        (str(tmp_path / "Cellar" / "ggml" / "*" / "libexec"),),
    )

    assert _find_ggml_gpu_backend(("metal",)) == str(version_dir / "libggml-metal.so")


def test_cr_ggml_backend_dirs_prepends(monkeypatch) -> None:
    monkeypatch.setenv("CR_GGML_BACKEND_DIRS", "/custom/one:/custom/two")

    dirs = backends._ggml_backend_dirs()

    assert dirs[:2] == ("/custom/one", "/custom/two")
    assert dirs[2:] == _GGML_BACKEND_DIRS


# --------------------------------------------------------------------------- #
# whisper-cli discovery: Homebrew bin fallback
# --------------------------------------------------------------------------- #
def test_find_whisper_cli_uses_homebrew_bin_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_WHISPER_CLI", raising=False)
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "whisper-cli").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(backends, "_WHISPER_CLI_BIN_DIRS", (str(bindir),))

    assert backends._find_whisper_cli() == str(bindir / "whisper-cli")
