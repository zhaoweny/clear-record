"""Platform-native base directories, resolved once with ``platformdirs`` (ADR-0025).

This is the **one** module that imports ``platformdirs``, and it sits at the
``clear_record`` package root — above the layer DAG — precisely because it must:
``clear_record.core`` may import no third-party package
(``tests/test_layering.py``), so ``core`` **receives** its directories from here
instead of computing them. Importing :mod:`clear_record` calls :func:`install`,
which installs the resolved defaults into :mod:`clear_record.core.paths`.

The legacy XDG locations are computed here too. They exist *only* so
:mod:`clear_record.core.paths` can adopt a pre-platformdirs install rather than
orphan it (ADR-0025); nothing resolves against them otherwise, and they are the
only XDG literals left in the codebase.

`platformdirs` returns platform-native paths: macOS ``~/Library/Application
Support``/``~/Library/Logs``, Windows ``%APPDATA%``/``%LOCALAPPDATA%``, Linux the
XDG directories.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
    user_log_dir,
    user_state_dir,
)

from clear_record.core.paths import APP, DefaultDirs, install_defaults


def _legacy(env_var: str, *fallback: str) -> Path:
    """The pre-platformdirs XDG app directory for one kind.

    ``$XDG_*_HOME`` when set, else the spec default under ``~``; the app name is
    appended so this matches exactly what the old hand-rolled resolvers returned.
    """
    override = os.environ.get(env_var)
    base = Path(override).expanduser() if override else Path.home().joinpath(*fallback)
    return base / APP


def default_dirs() -> DefaultDirs:
    """Resolve the platform-native defaults and the legacy XDG locations."""
    legacy_state = _legacy("XDG_STATE_HOME", ".local", "state")
    return DefaultDirs(
        data=Path(user_data_dir(APP)),
        config=Path(user_config_dir(APP)),
        cache=Path(user_cache_dir(APP)),
        state=Path(user_state_dir(APP)),
        logs=Path(user_log_dir(APP)),
        legacy_data=_legacy("XDG_DATA_HOME", ".local", "share"),
        legacy_config=_legacy("XDG_CONFIG_HOME", ".config"),
        legacy_cache=_legacy("XDG_CACHE_HOME", ".cache"),
        legacy_state=legacy_state,
        legacy_logs=legacy_state / "logs",
    )


def install() -> None:
    """Resolve and install the platform defaults (idempotent)."""
    install_defaults(default_dirs())


__all__ = ["default_dirs", "install"]
