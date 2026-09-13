"""clear_record.providers: per-vendor ASR backend adapters for clear-record.

Vendor stacks are selected behind a single ``Backend`` interface and are never
imported by ``clear_record.core``. A backend is *available* only if its optional
extra is installed and its runtime probe succeeds; otherwise the CLI reports it
as unavailable rather than failing the whole pipeline.
"""

from __future__ import annotations

from clear_record.providers.backends import (
    BACKENDS,
    PluginLoadProbe,
    available_backend_ids,
    get_backend,
    probe_ggml_plugin_load,
)
from clear_record.providers.base import (
    Backend,
    BackendBase,
    BackendId,
    BackendInfo,
    DEFAULT_MODEL,
)
from clear_record.providers.paths import resolve_models_dir
from clear_record.providers.process import (
    CancellableProcessRunner,
    ProcessCancelled,
    ProcessRunner,
    SubprocessRunner,
)

__all__ = [
    "BACKENDS",
    "Backend",
    "BackendBase",
    "BackendId",
    "BackendInfo",
    "CancellableProcessRunner",
    "DEFAULT_MODEL",
    "PluginLoadProbe",
    "ProcessCancelled",
    "ProcessRunner",
    "SubprocessRunner",
    "available_backend_ids",
    "get_backend",
    "probe_ggml_plugin_load",
    "resolve_models_dir",
]
