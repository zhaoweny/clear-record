"""App-owned state for the clear-record web/service surface.

This subpackage is the **headless service**: the project registry, the
per-project glossary, meetings and their tape sets, background pipeline runs
and the artifacts they produce. It is usable with no web app — an MCP client or
a script can import it directly, and the same operations would drive an agent.

Only *app-owned* state lives here. Recordings, workspaces and archives stay in
user-chosen directories (ADR-0006/ADR-0007); the registry stores paths and
metadata, never audio blobs.
"""

from __future__ import annotations

from clear_record.service.archive import archive_meeting, verify_archive
from clear_record.service.models import (
    MEETING_STATUSES,
    RUN_STATUSES,
    TERM_AUTHORS,
    TERM_STATUSES,
    Archive,
    Artifact,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
)
from clear_record.service.paths import (
    APP,
    REGISTRY_FILENAME,
    config_path,
    registry_path,
    resolve_data_dir,
)
from clear_record.service.runs import RunManager, RunState, collect_artifacts
from clear_record.service.store import SCHEMA_VERSION, Registry

__all__ = [
    "APP",
    "Archive",
    "Artifact",
    "GlossaryTerm",
    "MEETING_STATUSES",
    "Meeting",
    "PipelineRun",
    "Project",
    "REGISTRY_FILENAME",
    "RUN_STATUSES",
    "RecordingSet",
    "Registry",
    "RunManager",
    "RunState",
    "SCHEMA_VERSION",
    "TERM_AUTHORS",
    "TERM_STATUSES",
    "archive_meeting",
    "collect_artifacts",
    "config_path",
    "registry_path",
    "resolve_data_dir",
    "verify_archive",
]
