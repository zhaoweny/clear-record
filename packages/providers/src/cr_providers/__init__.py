"""cr-providers: per-vendor ASR backend adapters for clear-record.

Vendor stacks are selected behind a single ``Backend`` interface and are never
imported by ``cr_core``. A backend is *available* only if its optional extra is
installed and its runtime probe succeeds; otherwise the CLI reports it as
unavailable rather than failing the whole pipeline.
"""

from __future__ import annotations

from cr_providers.backends import (
    BACKENDS,
    available_backend_ids,
    get_backend,
)
from cr_providers.base import Backend, BackendId, BackendInfo, DEFAULT_MODEL

__all__ = [
    "BACKENDS",
    "Backend",
    "BackendId",
    "BackendInfo",
    "DEFAULT_MODEL",
    "available_backend_ids",
    "get_backend",
]
