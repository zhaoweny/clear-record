"""The one resolver for clear-record's app-owned directories (ADR-0025).

Precedence, everywhere:

    explicit argument  >  the ``CR_*`` variable  >  config file  >  platformdirs default

``platformdirs`` supplies the default of every location, with **platform-native**
paths: ``~/Library/Application Support`` (data/config/state) and ``~/Library/Logs``
(macOS), ``%APPDATA%``/``%LOCALAPPDATA%`` (Windows), the XDG directories (Linux).
This module is **stdlib-only**, because ``core`` may import no third-party
package (ADR-0012, ``tests/test_layering.py``). The platformdirs call itself
lives one layer up, in :mod:`clear_record._native_paths`, which resolves the
defaults and installs them here when :mod:`clear_record` is imported: ``core``
*receives* its directories, it never computes a platform path.

Only **config, data, cache, state and logs** are app-owned. A user-chosen
workspace (the CLI's ``--dir`` / a meeting's ``workspace_path``) is the user's
document, kept wherever they point (ADR-0007). The **managed** workspace root
(:func:`resolve_workspace_root`, ADR-0024) is app-owned *additionally* and
defaults under data.

The config file is a minimal TOML with a ``[paths]`` table::

    [paths]
    data_dir = "~/somewhere/clear-record"
    workspace_root = "~/tapes"       # optional; a NAS or a big disk

It lives at ``<config>/clear-record/config.toml`` and is also how a source
checkout expresses itself (ADR-0007): there is no separate "source mode" key.

**Migration (ADR-0025).** On first run an install that predates platformdirs has
its data under the old XDG location. When the native location does not exist and
the legacy one does, the legacy directory is *adopted* — used in place — and a
one-line notice names the native location it can be moved to. Nothing is moved,
so an interruption cannot lose a registry, a model or a multi-GB workspace.

The **models** directory has a second legacy shape. Before the ADR-0007
kind-split its default was ``<cwd>/models``. That location is adopted the same
way, but only when a recognizer installed from above — by the layer that owns the
artifact format — *positively identifies* it as a cache of our weights. Which
names identify them is that format's own knowledge, and the format belongs to a
vendor, so this module, vendor-free, consumes the recognizer
(:func:`install_models_recognizer`) rather than spelling the names itself:
``<cwd>`` is wherever the user happened to run the command, so a directory
merely *named* ``models`` is not evidence that any weights are there to lose.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from clear_record.core.i18n import tr

APP = "clear-record"
REGISTRY_FILENAME = "registry.sqlite3"
CONFIG_FILENAME = "config.toml"
#: File under the state dir that records the running node's address, so every
#: surface reaches it instead of scanning for a port (:mod:`clear_record.core.node`).
NODE_FILENAME = "node.json"
#: Directory under the data dir that holds a managed meeting's workspace
#: (ADR-0024). Kept here so the layout has one owner.
WORKSPACES_DIRNAME = "workspaces"
#: Directory under the data dir that holds downloaded model weights.
MODELS_DIRNAME = "models"
#: Directory under the cache dir that holds the resumable per-source chunk cache.
CHUNKS_DIRNAME = "chunks"

#: The documented ``CR_*`` escape hatches. ADR-0025 keeps them and their
#: precedence unchanged; they win over the config file and the platform default.
ENV_DATA_DIR = "CR_DATA_DIR"
ENV_STATE_DIR = "CR_STATE_DIR"
ENV_LOG_DIR = "CR_LOG_DIR"
ENV_CACHE_DIR = "CR_CACHE_DIR"
ENV_MODELS_DIR = "CR_MODELS_DIR"
ENV_WORKSPACE_ROOT = "CR_WORKSPACE_ROOT"


@dataclass(frozen=True)
class DefaultDirs:
    """The resolved platform-native bases, installed by ``clear_record``.

    Each ``*_dir`` already includes the application name (e.g.
    ``~/Library/Application Support/clear-record``). The ``legacy_*`` fields are
    the pre-platformdirs XDG locations, computed only so an existing install can
    be adopted rather than orphaned (ADR-0025); nothing else resolves against
    them.
    """

    data: Path
    config: Path
    cache: Path
    state: Path
    logs: Path
    legacy_data: Path
    legacy_config: Path
    legacy_cache: Path
    legacy_state: Path
    legacy_logs: Path


#: Injected once, at ``import clear_record``, by :mod:`clear_record._native_paths`.
_defaults: DefaultDirs | None = None
#: Labels whose adoption notice has already been printed, so it appears once.
_notified: set[str] = set()


def install_defaults(dirs: DefaultDirs) -> None:
    """Install the platformdirs-resolved defaults (called by the layer above)."""
    global _defaults
    _defaults = dirs


def _nothing_recognized(_directory: Path) -> bool:
    """No artifact format is known to this layer, so nothing is a cache of ours.

    The recognizer in force before the layer above installs the real one.
    """
    return False


#: The positive identity check for the legacy ``<cwd>/models`` candidate,
#: installed from the layer above (:mod:`clear_record.providers`). Which names
#: prove that a directory is a cache of *our* weights is the artifact format's
#: own knowledge, and that format belongs to a vendor: this module spells no
#: vendor name (ADR-0012, ``tests/test_layering.py``), so it *receives* the
#: recognizer and consumes it. Until one is installed nothing is recognized —
#: ``<cwd>`` is wherever the user ran the command, so a directory that merely
#: shares the name ``models`` is no evidence, and existence alone is exactly that
#: non-evidence.
_recognize_models: Callable[[Path], bool] = _nothing_recognized


def install_models_recognizer(recognize: Callable[[Path], bool]) -> None:
    """Install the recognizer for a legacy ``<cwd>/models`` (called from above)."""
    global _recognize_models
    _recognize_models = recognize


def _dirs() -> DefaultDirs:
    if _defaults is None:  # pragma: no cover - ``import clear_record`` installs
        raise RuntimeError(
            "clear-record app directories were not initialized; "
            "import clear_record before resolving a path"
        )
    return _defaults


def _cwd() -> Path:
    """The working directory — the base of the old ``<cwd>/models`` default.

    A seam, not a bare ``Path.cwd()`` call, so the legacy candidate can be aimed
    deliberately. The test suite points it away from the checkout, where a
    developer's real, gitignored ``models/`` cache lives.
    """
    return Path.cwd()


def _adopt(
    native: Path,
    legacy: Path | None,
    label: str,
    *,
    recognize: Callable[[Path], bool] | None = None,
    hint: str = "",
) -> Path:
    """Adopt the legacy location when only it exists; else return the native one.

    Used in place, never copied: an existing install must not appear to have
    lost its data, and an interrupted copy of a multi-GB workspace could. The
    notice says where the native location is, so the user can move it.

    *recognize* is an optional positive identity check. When given, a legacy
    directory that fails it is not adopted — necessary for ``<cwd>/models``,
    where existence alone proves nothing because ``<cwd>`` is wherever the user
    ran the command. *hint* appends one actionable sentence to the notice.
    """
    if legacy is None or native.exists() or not legacy.exists():
        return native
    if recognize is not None and not recognize(legacy):
        return native
    if label not in _notified:
        _notified.add(label)
        print(
            tr(
                "clear-record: adopting the existing pre-platformdirs {label} "
                "directory at {legacy}; move it to {native} for the "
                "platform-native layout (ADR-0025).{hint}",
                label=label,
                legacy=legacy,
                native=native,
                hint=hint,
            ),
            file=sys.stderr,
        )
    return legacy


# --- the config file ------------------------------------------------------ #


def resolve_config_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned config directory (no ``CR_*`` override)."""
    dirs = _dirs()
    if explicit:
        return Path(explicit).expanduser()
    return _adopt(dirs.config, dirs.legacy_config, "config")


def config_path() -> Path:
    """The app's TOML config path (may not exist)."""
    return resolve_config_dir() / CONFIG_FILENAME


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
    native: Path,
    legacy: Path | None = None,
    label: str | None = None,
    recognize: Callable[[Path], bool] | None = None,
    hint: str = "",
) -> Path:
    """One resolver for every app-owned directory (ADR-0025).

    explicit argument > the ``CR_*`` variable > the config file's key > the
    platformdirs default. Each resolver below names its variable/key/default, so
    the precedence lives here once instead of being restated (and potentially
    drifting) per directory.

    An override returns before :func:`_adopt`, so adoption — and its notice —
    never happens for an explicit flag, a ``CR_*`` variable or a config value: an
    explicit choice needs no migration.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser()
    configured = _config_value(config_key)
    if configured:
        return Path(configured).expanduser()
    return _adopt(native, legacy, label or config_key, recognize=recognize, hint=hint)


# --- the app-owned directories -------------------------------------------- #


def resolve_data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned data directory by the ADR-0025 precedence."""
    dirs = _dirs()
    return _resolve_app_dir(
        explicit,
        env_var=ENV_DATA_DIR,
        config_key="data_dir",
        native=dirs.data,
        legacy=dirs.legacy_data,
        label="data",
    )


def registry_path(explicit_data_dir: str | os.PathLike | None = None) -> Path:
    """The SQLite registry file inside the resolved data directory."""
    return resolve_data_dir(explicit_data_dir) / REGISTRY_FILENAME


def resolve_state_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned *state* directory by the ADR-0025 precedence."""
    dirs = _dirs()
    return _resolve_app_dir(
        explicit,
        env_var=ENV_STATE_DIR,
        config_key="state_dir",
        native=dirs.state,
        legacy=dirs.legacy_state,
        label="state",
    )


def node_address_path() -> Path:
    """The file where a running node records its address (may not exist).

    One path for the writer and the readers — the command line, the console, the
    MCP adapter and the tray — so no surface guesses a port.
    """
    return resolve_state_dir() / NODE_FILENAME


def resolve_cache_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the app-owned *cache* directory (the chunk cache root, ADR-0007)."""
    dirs = _dirs()
    return _resolve_app_dir(
        explicit,
        env_var=ENV_CACHE_DIR,
        config_key="cache_dir",
        native=dirs.cache,
        legacy=dirs.legacy_cache,
        label="cache",
    )


def resolve_logs_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the rotating diagnostics log directory.

    ``CR_LOG_DIR`` wins, then the config file's ``log_dir`` key; the platform
    default is last. Honours the legacy state location so an existing log trail
    is not orphaned.
    """
    dirs = _dirs()
    return _resolve_app_dir(
        explicit,
        env_var=ENV_LOG_DIR,
        config_key="log_dir",
        native=dirs.logs,
        legacy=dirs.legacy_logs,
        label="logs",
    )


def resolve_models_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the models directory: flag > ``CR_MODELS_DIR`` > config > ``<data>/models``.

    Models are app-owned *data* (ADR-0007's kind-split, ADR-0025); the old
    ``<cwd>/models`` default is gone, and a source checkout points at its own
    directory through the config file instead.

    A pre-move source checkout that still holds its weights in ``<cwd>/models``
    is adopted rather than silently re-downloaded — but only when the recognizer
    installed from above (:func:`install_models_recognizer`) identifies a cache
    there, because ``<cwd>`` is wherever the user happened to run the command.
    An override is checked first, so a pinned location neither adopts nor
    announces anything.
    """
    return _resolve_app_dir(
        explicit,
        env_var=ENV_MODELS_DIR,
        config_key="models_dir",
        native=resolve_data_dir() / MODELS_DIRNAME,
        legacy=_cwd() / MODELS_DIRNAME,
        label="models",
        recognize=_recognize_models,
        hint=tr(" Set CR_MODELS_DIR to pin a different models directory."),
    )


def resolve_workspace_root(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the **managed** workspace root (ADR-0024).

    A managed workspace is app-owned *additionally*: it holds the tapes a user
    uploaded to the console, so the console can transcribe them without the user
    placing files on the node first. It does **not** override ADR-0007's rule for
    the CLI's ``--dir``; that workspace stays a user document.

    Precedence: explicit argument > ``CR_WORKSPACE_ROOT`` > the config file's
    ``workspace_root`` > ``<data>/workspaces/``. The tapes are large, so
    ``CR_WORKSPACE_ROOT`` (a NAS or a dedicated disk) is the expected override.
    """
    return _resolve_app_dir(
        explicit,
        env_var=ENV_WORKSPACE_ROOT,
        config_key="workspace_root",
        native=resolve_data_dir() / WORKSPACES_DIRNAME,
        label="workspaces",
    )


__all__ = [
    "APP",
    "CHUNKS_DIRNAME",
    "CONFIG_FILENAME",
    "DefaultDirs",
    "ENV_CACHE_DIR",
    "ENV_DATA_DIR",
    "ENV_LOG_DIR",
    "ENV_MODELS_DIR",
    "ENV_STATE_DIR",
    "ENV_WORKSPACE_ROOT",
    "MODELS_DIRNAME",
    "NODE_FILENAME",
    "REGISTRY_FILENAME",
    "WORKSPACES_DIRNAME",
    "config_path",
    "install_defaults",
    "install_models_recognizer",
    "node_address_path",
    "registry_path",
    "resolve_cache_dir",
    "resolve_config_dir",
    "resolve_data_dir",
    "resolve_logs_dir",
    "resolve_models_dir",
    "resolve_state_dir",
    "resolve_workspace_root",
]
