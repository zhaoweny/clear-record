"""App-owned directory resolution for the clear-record service (ADR-0025).

The **one** resolver lives in :mod:`clear_record.core.paths`; this module is the
service-facing name for it, kept so the service surface (and its callers) keeps
a stable import path. ``core`` may import no third-party package
(``tests/test_layering.py``), so the platformdirs call lives one layer up in
:mod:`clear_record._native_paths`, which installs the platform-native defaults
when :mod:`clear_record` is imported. The precedence — explicit argument >
``CR_*`` variable > config file > platformdirs default — and every default are
therefore defined in exactly one place.

Only **config, data, cache, state and logs** are app-owned; a user-chosen
workspace and the archives are the user's documents, kept wherever the user
points (ADR-0007). The managed workspace root is app-owned *additionally*
(ADR-0024) and defaults under data.
"""

from __future__ import annotations

from clear_record.core.paths import (
    APP,
    CHUNKS_DIRNAME,
    CONFIG_FILENAME,
    ENV_CACHE_DIR,
    ENV_DATA_DIR,
    ENV_LOG_DIR,
    ENV_MODELS_DIR,
    ENV_STATE_DIR,
    ENV_WORKSPACE_ROOT,
    MODELS_DIRNAME,
    REGISTRY_FILENAME,
    WORKSPACES_DIRNAME,
    config_path,
    registry_path,
    resolve_cache_dir,
    resolve_config_dir,
    resolve_data_dir,
    resolve_logs_dir,
    resolve_models_dir,
    resolve_state_dir,
    resolve_workspace_root,
)

__all__ = [
    "APP",
    "CHUNKS_DIRNAME",
    "CONFIG_FILENAME",
    "ENV_CACHE_DIR",
    "ENV_DATA_DIR",
    "ENV_LOG_DIR",
    "ENV_MODELS_DIR",
    "ENV_STATE_DIR",
    "ENV_WORKSPACE_ROOT",
    "MODELS_DIRNAME",
    "REGISTRY_FILENAME",
    "WORKSPACES_DIRNAME",
    "config_path",
    "registry_path",
    "resolve_cache_dir",
    "resolve_config_dir",
    "resolve_data_dir",
    "resolve_logs_dir",
    "resolve_models_dir",
    "resolve_state_dir",
    "resolve_workspace_root",
]
