"""App-owned directory resolution for the clear-record service.

Precedence (ADR-0007, ADR-0013):

    explicit argument  >  ``CR_DATA_DIR``  >  config file  >  XDG default

The XDG Base Directory spec is followed on every platform. Only **config,
data, cache and state** are app-owned; the user's recordings, workspaces and
archives are the user's documents, kept wherever the user points.

The config file is a minimal TOML at ``$XDG_CONFIG_HOME/clear-record/config.toml``
and carries a ``[paths]`` table::

    [paths]
    data_dir = "~/somewhere/clear-record"

A config is also how a source checkout expresses itself (ADR-0007): there is no
separate "source mode" key.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from clear_record.core.diagnostics import logs_dir as _core_logs_dir
from clear_record.core.diagnostics import xdg_state_home as _core_xdg_state_home

APP = "clear-record"
REGISTRY_FILENAME = "registry.sqlite3"


def xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME`` or its spec default."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_data_home() -> Path:
    """``$XDG_DATA_HOME`` or its spec default."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def xdg_state_home() -> Path:
    """``$XDG_STATE_HOME`` or its spec default.

    The XDG *state* default is owned by ``core.diagnostics`` (the sink resolves
    it too, and ``core`` may not import ``service``); this is the service-facing
    name for the same one implementation.
    """
    return _core_xdg_state_home()


def config_path() -> Path:
    """The app's TOML config path (may not exist)."""
    return xdg_config_home() / APP / "config.toml"


def _config_paths() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    paths = data.get("paths")
    return paths if isinstance(paths, dict) else {}


def _config_value(key: str) -> str | None:
    value = _config_paths().get(key)
    return str(value) if value else None


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned data directory by the ADR-0007 precedence."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CR_DATA_DIR")
    if env:
        return Path(env).expanduser()
    configured = _config_value("data_dir")
    if configured:
        return Path(configured).expanduser()
    return xdg_data_home() / APP


def registry_path(explicit_data_dir: str | os.PathLike | None = None) -> Path:
    """The SQLite registry file inside the resolved data directory."""
    return resolve_data_dir(explicit_data_dir) / REGISTRY_FILENAME


def resolve_state_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned *state* directory by the ADR-0007 precedence.

    Same shape as :func:`resolve_data_dir`: explicit argument > ``CR_STATE_DIR``
    > the config file's ``state_dir`` > the XDG state default. ADR-0007 assigns
    logs and resume state to *state*, so this is where the rotating log sink
    lives — not under data or cache.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CR_STATE_DIR")
    if env:
        return Path(env).expanduser()
    configured = _config_value("state_dir")
    if configured:
        return Path(configured).expanduser()
    return xdg_state_home() / APP


def logs_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the rotating diagnostics log directory.

    Delegates to the sink's one resolver in ``core.diagnostics`` (explicit
    argument > ``CR_LOG_DIR`` > ``<state>/logs``), so a writer and a reader can
    never disagree about where the log lives. It is the sink's location, not a
    general app path, which is why the config file does not carry it.
    """
    return _core_logs_dir(explicit)


__all__ = [
    "APP",
    "REGISTRY_FILENAME",
    "config_path",
    "logs_dir",
    "registry_path",
    "resolve_data_dir",
    "resolve_state_dir",
    "xdg_config_home",
    "xdg_data_home",
    "xdg_state_home",
]
