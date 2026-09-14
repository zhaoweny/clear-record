"""clear_record.core: backend-agnostic application core for clear-record.

This package must stay free of vendor-specific or ML-framework-specific code
(CUDA, ROCm, Metal/CoreML, torch, tensorflow, a specific ASR library). It owns
the domain model and the *shape* of the pipeline; vendor adapters live in
``clear_record.providers`` and are selected behind an interface, never imported here.
"""

from __future__ import annotations

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
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    PROFILE_CUSTOM,
    PROFILES,
    RESOLVABLE_FIELDS,
    PipelineOptions,
    profile_values,
    resolve_options,
)
from clear_record.core.pipeline import PipelineSpec, PipelineStage, Step, pipeline_spec

__all__ = [
    "Alignment",
    "DECODER_KNOB_FIELDS",
    "DEFAULT_CHUNK_S",
    "DEFAULT_OVERLAP_S",
    "EventSink",
    "JobEvent",
    "PROFILE_CUSTOM",
    "PROFILES",
    "RESOLVABLE_FIELDS",
    "PipelineOptions",
    "PipelineSpec",
    "PipelineStage",
    "Progress",
    "RecordDocument",
    "Segment",
    "Source",
    "Step",
    "TranscriptionResult",
    "alignment_from_dict",
    "emit",
    "load_json",
    "pipeline_spec",
    "profile_values",
    "record_from_dict",
    "resolve_options",
    "segment_from_dict",
    "source_from_dict",
    "to_dict",
    "write_json",
]
