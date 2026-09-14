"""App-owned state for the clear-record web/service surface.

This subpackage is the **headless service**: the project registry, the
per-project glossary, and (in later slices) runs, archives and agent tasks. It
is usable with no web app — an MCP client or a script can import it directly.

Only *app-owned* state lives here. Recordings, archives and derived records stay
in user-chosen directories (ADR-0006/ADR-0007); the registry stores paths and
metadata, never audio blobs.
"""

from __future__ import annotations

from clear_record.service.models import GlossaryTerm, Project, TERM_STATUSES
from clear_record.service.paths import (
    APP,
    REGISTRY_FILENAME,
    config_path,
    registry_path,
    resolve_data_dir,
)
from clear_record.service.store import SCHEMA_VERSION, Registry

__all__ = [
    "APP",
    "GlossaryTerm",
    "Project",
    "REGISTRY_FILENAME",
    "Registry",
    "SCHEMA_VERSION",
    "TERM_STATUSES",
    "config_path",
    "registry_path",
    "resolve_data_dir",
]
