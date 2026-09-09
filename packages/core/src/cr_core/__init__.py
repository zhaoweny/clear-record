"""cr-core: backend-agnostic application core for clear-record.

This package must stay free of vendor-specific or ML-framework-specific code
(CUDA, ROCm, Metal/CoreML, torch, tensorflow, a specific ASR library). It owns
the domain model and the *shape* of the pipeline; vendor adapters live in
``cr_providers`` and are selected behind an interface, never imported here.
"""

from __future__ import annotations

from cr_core.pipeline import PipelineSpec, Step, pipeline_spec

__all__ = ["PipelineSpec", "Step", "pipeline_spec"]
