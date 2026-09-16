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


def test_legacy_state_cache_and_logs_use_the_old_layout(tmp_path, monkeypatch) -> None:
    """The three non-XDG-var kinds resolve under their spec defaults too.

    Only the legacy paths are asserted; the platform-native data/config legacy
    values are pinned above.
    """
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path))
    dirs = _native_paths.default_dirs()
    assert dirs.legacy_cache == tmp_path / "cache" / "clear-record"
    assert dirs.legacy_state == tmp_path / "state" / "clear-record"
    assert dirs.legacy_logs == tmp_path / "state" / "clear-record" / "logs"
    assert dirs.legacy_data == tmp_path / ".local" / "share" / "clear-record"
