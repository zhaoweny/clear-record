"""cr-core: backend-agnostic application core for clear-record.

This package must stay free of vendor-specific or ML-framework-specific code
(CUDA, ROCm, Metal/CoreML, torch, tensorflow, a specific ASR library). It owns
the domain model and the *shape* of the pipeline; vendor adapters live in
``cr_providers`` and are selected behind an interface, never imported here.
"""

from __future__ import annotations

from cr_core.model import (
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
from cr_core.pipeline import PipelineSpec, PipelineStage, Step, pipeline_spec

__all__ = [
    "Alignment",
    "PipelineSpec",
    "PipelineStage",
    "RecordDocument",
    "Segment",
    "Source",
    "Step",
    "TranscriptionResult",
    "alignment_from_dict",
    "load_json",
    "pipeline_spec",
    "record_from_dict",
    "segment_from_dict",
    "source_from_dict",
    "to_dict",
    "write_json",
]
