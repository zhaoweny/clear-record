"""App-owned directory resolution for the clear-record service.

Precedence (ADR-0007, ADR-0013, ADR-0024):

    explicit argument  >  the ``CR_*`` variable  >  config file  >  default

The XDG Base Directory spec is followed on every platform. Only **config,
data, cache and state** are app-owned; a user-chosen workspace and the archives
are the user's documents, kept wherever the user points.

One exception is app-owned by *addition*, not by replacement: the **managed
workspace root** (:func:`resolve_workspace_root`, ADR-0024) is an app-owned
directory under data that only a meeting created in the console uses. ADR-0007
still governs the CLI's ``--dir`` / a meeting's user-chosen ``workspace_path``.

The config file is a minimal TOML at ``$XDG_CONFIG_HOME/clear-record/config.toml``
and carries a ``[paths]`` table::

    [paths]
    data_dir = "~/somewhere/clear-record"
    workspace_root = "~/tapes"       # optional; a NAS or a big disk

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
#: Directory under the data dir that holds a managed meeting's workspace
#: (ADR-0024). Kept here so the layout has one owner.
WORKSPACES_DIRNAME = "workspaces"


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


def _resolve_app_dir(
    explicit: str | os.PathLike | None,
    *,
    env_var: str,
    config_key: str,
    default: Path,
) -> Path:
    """One resolver for every app-owned directory (ADR-0007).

    explicit argument > the ``CR_*`` variable > the config file's key > default.
    Each resolver below names its variable/key and supplies its default, so the
    precedence lives here once instead of being restated (and potentially
    drifting) per directory.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser()
    configured = _config_value(config_key)
    if configured:
        return Path(configured).expanduser()
    return default


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned data directory by the ADR-0007 precedence."""
    return _resolve_app_dir(
        explicit,
        env_var="CR_DATA_DIR",
        config_key="data_dir",
        default=xdg_data_home() / APP,
    )


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
    return _resolve_app_dir(
        explicit,
        env_var="CR_STATE_DIR",
        config_key="state_dir",
        default=xdg_state_home() / APP,
    )


def resolve_workspace_root(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the **managed** workspace root (ADR-0024).

    A managed workspace is app-owned *additionally*: it holds the tapes a user
    uploaded to the console, so the console can transcribe them without the
    user placing files on the node first. It does **not** override ADR-0007's
    rule for the CLI's ``--dir``; that workspace stays a user document.

    Precedence, the same shape as the other app-owned directories: explicit
    argument > ``CR_WORKSPACE_ROOT`` > the config file's ``workspace_root`` >
    ``<data>/workspaces/``. The tapes are large, so ``CR_WORKSPACE_ROOT``
    (a NAS or a dedicated disk) is the expected override.
    """
    return _resolve_app_dir(
        explicit,
        env_var="CR_WORKSPACE_ROOT",
        config_key="workspace_root",
        default=resolve_data_dir() / WORKSPACES_DIRNAME,
    )


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
    "WORKSPACES_DIRNAME",
    "config_path",
    "logs_dir",
    "registry_path",
    "resolve_data_dir",
    "resolve_state_dir",
    "resolve_workspace_root",
    "xdg_config_home",
    "xdg_data_home",
    "xdg_state_home",
]
