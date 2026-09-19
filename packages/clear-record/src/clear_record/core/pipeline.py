"""The pipeline shape for clear-record.

The public workflow, carried over from the original concept, is:

    ingest -> align -> transcribe -> reconcile -> export

The core only *declares* the shape and each step's contract. Executing a step
against real audio and a real ASR backend is the job of a provider (see
``clear_record.providers``) wired in by the CLI.

This declaration is the **single source of stage truth**: the CLI builds one
subcommand per stage from it and the full-pipeline ``run`` iterates it, so the
order and the stage names live here once. Adding a stage is one edit above, plus
its implementation.
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
class PipelineStage:
    """One declared stage: its identity and the CLI metadata it carries."""

    step: Step
    help: str


@dataclass(frozen=True)
class PipelineSpec:
    """The single declaration of the pipeline: stage order and CLI metadata.

    The CLI builds one subcommand per :attr:`stages` entry, in order, and the
    full-pipeline ``run`` iterates the same sequence; neither repeats the order.
    """

    name: str
    stages: Sequence[PipelineStage]
    description: str

    @property
    def steps(self) -> tuple[Step, ...]:
        """The ordered stage identities."""
        return tuple(stage.step for stage in self.stages)

    def cli_commands(self) -> tuple[str, ...]:
        """The ordered subcommand names, one per stage."""
        return tuple(stage.step.value for stage in self.stages)

    def run_help(self) -> str:
        """Help text for the `run` subcommand, derived from the stage order."""
        return "full pipeline: " + " -> ".join(self.cli_commands())


_STAGES: Final[tuple[PipelineStage, ...]] = (
    PipelineStage(Step.INGEST, "discover/declare recording sources"),
    PipelineStage(Step.ALIGN, "estimate source time offsets onto a common clock"),
    PipelineStage(Step.TRANSCRIBE, "run a chosen ASR backend over each source"),
    PipelineStage(
        Step.RECONCILE, "merge segments into an attributed, aligned timeline"
    ),
    PipelineStage(Step.EXPORT, "write Markdown/SRT/VTT/JSON artifacts"),
)

_DESCRIPTION: Final[str] = (
    "From many recordings to one clear record: ingest several audio sources, "
    "align them onto a common clock, transcribe the result, reconcile it into "
    "an attributable record, then export a searchable/archiveable artifact."
)


def pipeline_spec() -> PipelineSpec:
    """The canonical pipeline description used by the CLI and docs."""
    return PipelineSpec(name="clear-record", stages=_STAGES, description=_DESCRIPTION)


class RunCancelled(Exception):
    """A run stopped because its cancel was requested (RUN-04).

    Raised where a pipeline checks its cancel signal — at a stage boundary in
    :func:`clear_record.cli.stages.run`, and between chunks inside the transcribe
    pool — so a cancellation lands on a boundary the work can be stopped at,
    never mid-write. The run's owner turns it into the run's terminal ``stopped``
    state; nothing else catches it, and a caller that is not running a pipeline
    (the CLI's own Ctrl-C path) never sees it.
    """
