"""The one resolver and its ADR-0025 legacy-install migration.

The resolver is :mod:`clear_record.core.paths`; it is stdlib-only and receives
its platformdirs defaults from :mod:`clear_record._native_paths`. These tests
pin the precedence, every default, and — the reason migration is part of the
change — that an existing pre-platformdirs install is **adopted** rather than
orphaned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.core import paths


def _dirs(tmp_path: Path, *, legacy: bool = False) -> paths.DefaultDirs:
    """Test defaults; the legacy bases are fake dirs, absent unless asked for."""
    legacy_root = tmp_path / "legacy"
    if legacy:
        for name in ("data", "config", "cache", "state", "logs"):
            (legacy_root / name).mkdir(parents=True)
    return paths.DefaultDirs(
        data=tmp_path / "native" / "data",
        config=tmp_path / "native" / "config",
        cache=tmp_path / "native" / "cache",
        state=tmp_path / "native" / "state",
        logs=tmp_path / "native" / "logs",
        legacy_data=legacy_root / "data",
        legacy_config=legacy_root / "config",
        legacy_cache=legacy_root / "cache",
        legacy_state=legacy_root / "state",
        legacy_logs=legacy_root / "logs",
    )


@pytest.fixture()
def install(monkeypatch):
    """Install test defaults and reset the once-per-process notice bookkeeping."""

    def _install(dirs: paths.DefaultDirs) -> None:
        monkeypatch.setattr(paths, "_defaults", dirs)
        monkeypatch.setattr(paths, "_notified", set())

    return _install


# --- precedence ------------------------------------------------------------ #
def test_explicit_beats_env_config_and_default(tmp_path, install, monkeypatch) -> None:
    install(_dirs(tmp_path))
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "env-state"))
    assert paths.resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"
    assert paths.resolve_state_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_every_default_is_platform_native(tmp_path, install, monkeypatch) -> None:
    install(_dirs(tmp_path))
    assert paths.resolve_data_dir() == tmp_path / "native" / "data"
    assert paths.resolve_config_dir() == tmp_path / "native" / "config"
    assert paths.config_path() == tmp_path / "native" / "config" / "config.toml"
    assert paths.resolve_cache_dir() == tmp_path / "native" / "cache"
    assert paths.resolve_state_dir() == tmp_path / "native" / "state"
    assert paths.resolve_logs_dir() == tmp_path / "native" / "logs"


def test_models_and_workspace_root_default_under_data(tmp_path, install) -> None:
    install(_dirs(tmp_path))
    assert paths.resolve_models_dir() == tmp_path / "native" / "data" / "models"
    # The workspace root defaults under data and is still not app-owned content.
    assert paths.resolve_workspace_root() == tmp_path / "native" / "data" / "workspaces"


# --- migration: adopt the legacy install ----------------------------------- #
def test_legacy_data_is_adopted_when_native_is_absent(
    tmp_path, install, monkeypatch, capsys
) -> None:
    install(_dirs(tmp_path, legacy=True))
    assert paths.resolve_data_dir() == tmp_path / "legacy" / "data"

    notice = capsys.readouterr().err
    assert "adopting" in notice
    assert str(tmp_path / "legacy" / "data") in notice
    assert str(tmp_path / "native" / "data") in notice


def test_the_adoption_notice_is_printed_once(tmp_path, install, capsys) -> None:
    install(_dirs(tmp_path, legacy=True))
    paths.resolve_data_dir()
    assert "adopting" in capsys.readouterr().err
    # A second resolution adopts the same locations again but says nothing new.
    paths.resolve_data_dir()
    assert capsys.readouterr().err == ""


def test_native_wins_when_both_locations_exist(tmp_path, install, capsys) -> None:
    install(_dirs(tmp_path, legacy=True))
    (tmp_path / "native" / "data").mkdir(parents=True)
    assert paths.resolve_data_dir() == tmp_path / "native" / "data"
    assert " data directory" not in capsys.readouterr().err


def test_no_adoption_when_neither_location_exists(tmp_path, install, capsys) -> None:
    install(_dirs(tmp_path))
    assert paths.resolve_data_dir() == tmp_path / "native" / "data"
    assert "adopting" not in capsys.readouterr().err


def test_an_override_beats_adoption_without_a_notice(
    tmp_path, install, monkeypatch, capsys
) -> None:
    install(_dirs(tmp_path, legacy=True))
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    assert paths.resolve_data_dir() == tmp_path / "env"
    assert "adopting" not in capsys.readouterr().err


def test_state_and_logs_adopt_the_legacy_locations(tmp_path, install, capsys) -> None:
    install(_dirs(tmp_path, legacy=True))
    assert paths.resolve_state_dir() == tmp_path / "legacy" / "state"
    assert paths.resolve_logs_dir() == tmp_path / "legacy" / "logs"
    err = capsys.readouterr().err
    # Config is adopted on the config read that every resolution begins with;
    # state and logs each announce themselves too.
    assert " state directory" in err
    assert " logs directory" in err


def test_models_adopt_under_the_adopted_data_dir(tmp_path, install, capsys) -> None:
    """A model that lives beside the adopted data must not look lost."""
    install(_dirs(tmp_path, legacy=True))
    assert paths.resolve_models_dir() == tmp_path / "legacy" / "data" / "models"
    # Models follow the adopted data dir; only data (and config) are announced.
    assert " data directory" in capsys.readouterr().err


def test_a_config_value_still_beats_the_default(tmp_path, install, monkeypatch) -> None:
    install(_dirs(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{tmp_path / "from-config"}"\n'
        f'models_dir = "{tmp_path / "cfg-models"}"\n'
    )
    monkeypatch.setattr(paths, "config_path", lambda: config)
    assert paths.resolve_data_dir() == tmp_path / "from-config"
    assert paths.resolve_models_dir() == tmp_path / "cfg-models"
