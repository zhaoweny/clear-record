"""The platformdirs boundary at the package root (ADR-0025).

``clear_record.core`` may import no third-party package, so
:mod:`clear_record._native_paths` is the one module that imports ``platformdirs``
and hands the resolved defaults down. These tests pin the *legacy* XDG formula,
which is what detects an existing pre-platformdirs install to adopt.
"""

from __future__ import annotations

from pathlib import Path

from clear_record import _native_paths


def test_legacy_uses_the_xdg_override_when_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert _native_paths._legacy("XDG_DATA_HOME", ".local", "share") == (
        tmp_path / "xdg" / "clear-record"
    )


def test_legacy_falls_back_to_the_spec_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _native_paths._legacy("XDG_CONFIG_HOME", ".config") == (
        tmp_path / ".config" / "clear-record"
    )


def test_default_dirs_include_every_kind_and_its_legacy() -> None:
    dirs = _native_paths.default_dirs()
    for name in ("data", "config", "cache", "state", "logs"):
        assert isinstance(getattr(dirs, name), Path)
        assert isinstance(getattr(dirs, f"legacy_{name}"), Path)
