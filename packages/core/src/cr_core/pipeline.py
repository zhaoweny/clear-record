"""The pipeline shape for clear-record.

The public workflow, carried over from the original concept, is:

    ingest -> align -> transcribe -> reconcile -> export

The core only *declares* the shape and each step's contract. Executing a step
against real audio and a real ASR backend is the job of a provider (see
``cr_providers``) wired in by the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Sequence


class Step(str, Enum):
    """Named stages of the clear-record pipeline (stable order)."""

    INGEST = "ingest"
    ALIGN = "align"
    TRANSCRIBE = "transcribe"
    RECONCILE = "reconcile"
    EXPORT = "export"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class PipelineSpec:
    """Describes the pipeline stages and the CLI subcommands they map to."""

    name: str
    steps: Sequence[Step]
    description: str

    def cli_commands(self) -> tuple[str, ...]:
        return tuple(step.value for step in self.steps)


DEFAULT_STEPS: Final[tuple[Step, ...]] = (
    Step.INGEST,
    Step.ALIGN,
    Step.TRANSCRIBE,
    Step.RECONCILE,
    Step.EXPORT,
)

_DESCRIPTION: Final[str] = (
    "From many recordings to one clear record: ingest several audio sources, "
    "align them onto a common clock, transcribe the result, reconcile it into "
    "an attributable record, then export a searchable/archiveable artifact."
)


def pipeline_spec() -> PipelineSpec:
    """The canonical pipeline description used by the CLI and docs."""
    return PipelineSpec(
        name="clear-record", steps=DEFAULT_STEPS, description=_DESCRIPTION
    )
