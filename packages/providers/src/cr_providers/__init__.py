"""cr-providers: per-vendor ASR backend adapters for clear-record.

Vendor stacks are selected behind a single ``Backend`` interface and are never
imported by ``cr_core``. A backend is *available* only if its optional extra is
installed and its runtime probe succeeds; otherwise the CLI reports it as
unavailable rather than failing the whole pipeline.
"""

from __future__ import annotations

from cr_providers.backends import (
    BACKENDS,
    PluginLoadProbe,
    available_backend_ids,
    get_backend,
    probe_ggml_plugin_load,
)
from cr_providers.base import (
    Backend,
    BackendBase,
    BackendId,
    BackendInfo,
    DEFAULT_MODEL,
)
from cr_providers.paths import resolve_models_dir
from cr_providers.process import (
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
