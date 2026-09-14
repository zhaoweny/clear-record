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
from clear_record.service.glossary import (
    CONFIRMED,
    GlossarySnapshot,
    build_snapshot,
    canonical_terms,
    project_snapshot,
    snapshot_from_text,
    write_project_snapshot,
    write_snapshot,
)
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
from clear_record.service.runs import (
    PipelineOptions,
    RunManager,
    RunState,
    collect_artifacts,
)
from clear_record.service.store import SCHEMA_VERSION, Registry
from clear_record.service.transcript import TranscriptSlice, read_transcript
from clear_record.service.webhooks import (
    ALL_EVENTS,
    ARCHIVE_CREATED,
    EMITTED_EVENTS,
    FUTURE_EVENTS,
    RUN_FAILED,
    RUN_FINISHED,
    RUN_STARTED,
    SIGNATURE_HEADER,
    TRANSCRIPT_READY,
    Delivery,
    WebhookConfig,
    WebhookEmitter,
    WebhookEndpoint,
    WebhookEvent,
    load_webhook_config,
    resolve_endpoints,
    sign,
)

__all__ = [
    "ALL_EVENTS",
    "APP",
    "ARCHIVE_CREATED",
    "Archive",
    "Artifact",
    "CONFIRMED",
    "Delivery",
    "EMITTED_EVENTS",
    "FUTURE_EVENTS",
    "GlossarySnapshot",
    "GlossaryTerm",
    "MEETING_STATUSES",
    "Meeting",
    "PipelineOptions",
    "PipelineRun",
    "Project",
    "REGISTRY_FILENAME",
    "RUN_FAILED",
    "RUN_FINISHED",
    "RUN_STARTED",
    "RUN_STATUSES",
    "RecordingSet",
    "Registry",
    "RunManager",
    "RunState",
    "SCHEMA_VERSION",
    "SIGNATURE_HEADER",
    "TERM_AUTHORS",
    "TERM_STATUSES",
    "TRANSCRIPT_READY",
    "TranscriptSlice",
    "WebhookConfig",
    "WebhookEmitter",
    "WebhookEndpoint",
    "WebhookEvent",
    "archive_meeting",
    "build_snapshot",
    "canonical_terms",
    "collect_artifacts",
    "config_path",
    "load_webhook_config",
    "project_snapshot",
    "read_transcript",
    "registry_path",
    "resolve_data_dir",
    "resolve_endpoints",
    "sign",
    "snapshot_from_text",
    "verify_archive",
    "write_project_snapshot",
    "write_snapshot",
]
