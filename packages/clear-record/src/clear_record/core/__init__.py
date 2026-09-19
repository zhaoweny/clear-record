"""clear_record.core: backend-agnostic application core for clear-record.

This package must stay free of vendor-specific or ML-framework-specific code
(CUDA, ROCm, Metal/CoreML, torch, tensorflow, a specific ASR library). It owns
the domain model and the *shape* of the pipeline; vendor adapters live in
``clear_record.providers`` and are selected behind an interface, never imported here.
"""

from __future__ import annotations

from clear_record.core.diagnostics import (
    DEFAULT_LEVEL,
    DEFAULT_LOG_LINES,
    ENV_LOG_LEVEL,
    LEVELS,
    LOG_FILENAME,
    effective_level,
    log_event,
    log_path,
    logs_dir,
    read_recent,
    set_level,
)
from clear_record.core.events import EventSink, JobEvent, Progress, emit
from clear_record.core.model import (
    Alignment,
    RecordDocument,
    Segment,
    Source,
    TranscriptionResult,
    alignment_from_dict,
    load_json,
    record_from_dict,
    segment_from_dict,
    source_from_dict,
    to_dict,
    write_json,
)
from clear_record.core.options import (
    DECODER_KNOB_FIELDS,
    DECODER_KNOBS,
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    PROFILE_CUSTOM,
    PROFILES,
    RESOLVABLE_FIELDS,
    RUN_KNOBS,
    DecoderKnobs,
    PipelineOptions,
    RunKnob,
    profile_values,
    resolve_options,
)
from clear_record.core.pipeline import (
    PipelineSpec,
    PipelineStage,
    RunCancelled,
    Step,
    pipeline_spec,
)
from clear_record.core.scope import (
    ChunkScope,
    ScopeError,
    format_seconds,
    parse_time_range,
)

__all__ = [
    "Alignment",
    "ChunkScope",
    "DECODER_KNOB_FIELDS",
    "DECODER_KNOBS",
    "DEFAULT_CHUNK_S",
    "DEFAULT_LEVEL",
    "DEFAULT_LOG_LINES",
    "DEFAULT_OVERLAP_S",
    "DecoderKnobs",
    "ENV_LOG_LEVEL",
    "EventSink",
    "JobEvent",
    "LEVELS",
    "LOG_FILENAME",
    "PROFILE_CUSTOM",
    "PROFILES",
    "RESOLVABLE_FIELDS",
    "RUN_KNOBS",
    "PipelineOptions",
    "PipelineSpec",
    "PipelineStage",
    "Progress",
    "RecordDocument",
    "RunCancelled",
    "RunKnob",
    "ScopeError",
    "Segment",
    "Source",
    "Step",
    "TranscriptionResult",
    "alignment_from_dict",
    "effective_level",
    "emit",
    "format_seconds",
    "load_json",
    "log_event",
    "log_path",
    "logs_dir",
    "parse_time_range",
    "pipeline_spec",
    "profile_values",
    "read_recent",
    "record_from_dict",
    "resolve_options",
    "segment_from_dict",
    "set_level",
    "source_from_dict",
    "to_dict",
    "write_json",
]
