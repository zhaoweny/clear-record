"""Data-directory resolution follows the ADR-0025 precedence.

    explicit  >  CR_DATA_DIR  >  config file  >  platformdirs default

Resolution now goes through ``platformdirs``, whose platform-native default is
injected by the package root and overridden by the hermetic ``conftest`` fixture.
No real config or environment leaks in: the module-level ``config_path`` is
redirected to a path that does not exist unless a test supplies one.
"""

from __future__ import annotations

from clear_record.core import paths as core_paths
from clear_record.service import paths


def test_explicit_argument_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    assert paths.resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_env_beats_config_and_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "env"))
    assert paths.resolve_data_dir() == tmp_path / "env"


def test_config_beats_default(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{tmp_path / "from-config"}"\n')
    monkeypatch.setattr(core_paths, "config_path", lambda: config)
    assert paths.resolve_data_dir() == tmp_path / "from-config"


def test_platform_default(tmp_path) -> None:
    """With no override, the injected platformdirs default is used."""
    assert paths.resolve_data_dir() == tmp_path / "app" / "data"


def test_malformed_config_is_ignored(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text("this is not toml = = =")
    monkeypatch.setattr(core_paths, "config_path", lambda: config)
    assert paths.resolve_data_dir() == tmp_path / "app" / "data"


def test_config_path_lives_in_the_config_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    assert paths.config_path() == tmp_path / "app" / "config" / "config.toml"


def test_registry_path_is_inside_the_data_dir(tmp_path) -> None:
    assert paths.registry_path(tmp_path) == tmp_path / "registry.sqlite3"
