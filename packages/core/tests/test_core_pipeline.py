"""Smoke tests for cr-core: the pipeline shape is declared, not executed."""

from __future__ import annotations

from cr_core import PipelineSpec, Step, pipeline_spec


def test_pipeline_spec_exposes_five_steps() -> None:
    spec: PipelineSpec = pipeline_spec()
    assert spec.name == "clear-record"
    assert spec.steps == (
        Step.INGEST,
        Step.ALIGN,
        Step.TRANSCRIBE,
        Step.RECONCILE,
        Step.EXPORT,
    )


def test_cli_commands_from_steps() -> None:
    spec = pipeline_spec()
    assert spec.cli_commands() == (
        "ingest",
        "align",
        "transcribe",
        "reconcile",
        "export",
    )
