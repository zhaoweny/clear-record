"""Data-directory resolution follows the ADR-0007 precedence.

    explicit  >  CR_DATA_DIR  >  config file  >  XDG default

No real config or environment leaks in: the module-level ``config_path`` is
redirected to a path that does not exist unless a test supplies one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.service import paths


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "absent.toml")


def test_explicit_argument_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    assert paths.resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_env_beats_config_and_xdg(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    assert paths.resolve_data_dir() == tmp_path / "env"


def test_config_beats_xdg(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{tmp_path / "from-config"}"\n')
    monkeypatch.setattr(paths, "config_path", lambda: config)
    assert paths.resolve_data_dir() == tmp_path / "from-config"


def test_xdg_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.resolve_data_dir() == tmp_path / "xdg" / "clear-record"


def test_home_default_when_xdg_unset(monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.resolve_data_dir() == Path.home() / ".local" / "share" / "clear-record"


def test_malformed_config_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "config.toml"
    config.write_text("this is not toml = = =")
    monkeypatch.setattr(paths, "config_path", lambda: config)
    assert paths.resolve_data_dir() == tmp_path / "xdg" / "clear-record"


def test_registry_path_is_inside_the_data_dir(tmp_path) -> None:
    assert paths.registry_path(tmp_path) == tmp_path / "registry.sqlite3"
