"""The four axes of a run -- the one place they are computed.

BENCH-01: everything a benchmark shows is derived, at display time, from the run
record, on four axes and no fifth:

* **accuracy** -- WER against a reference transcript when one is configured,
  otherwise coverage and mean confidence. The arithmetic is the CLI's
  (:func:`clear_record.cli.stages.calibration_report`), reused verbatim, so the
  terminal report and the console cannot disagree.
* **speed** -- audio seconds over total wall seconds, presented as x-realtime.
  Both primitives come from the run's cost record (RUN-01); the ratio itself is
  never stored.
* **memory** -- the peak RSS of the transcribe stage's decoder workers, measured
  while they ran (see :mod:`clear_record.cli.transcription`). A platform that
  cannot measure it reports ``None`` with a reason, never zero.
* **fit** -- what ``--auto`` chose and the facts it had to work with, read back
  from the run meta the resolvers recorded.

Every axis is a dict, and an axis whose primitives are missing carries a
``reason`` (a message ID; render it with ``tr``) instead of a number. A run
recorded before the cost record existed, a run that failed before its first
stage, or no run at all yields the same four axes -- never an exception.

Layering: this is a ``service`` module, so it may import ``cli`` (it reuses the
CLI's calibration arithmetic and the workspace reader). The ``bench`` subcommand
is registered through the ``clear_record.commands`` entry point, so
``clear_record.cli`` never imports this module (ADR-0013) and the terminal and
the console render the *same* dict.
"""

from __future__ import annotations

from pathlib import Path

import click

from clear_record.cli import stages
from clear_record.cli.workspace import Workspace
from clear_record.core.i18n import deferred, tr
from clear_record.service.models import PipelineRun
from clear_record.service.paths import registry_path
from clear_record.service.runs import cost_of, int_or_none, number_or_none
from clear_record.service.store import Registry

#: Why an axis has no number. These are message IDs (:func:`deferred`): each
#: surface translates them with ``tr`` at its own boundary.
NO_WORKSPACE = deferred("the run has no workspace to measure")
NO_RECORD = deferred("the workspace has no readable record to score")
NO_COST_RECORD = deferred("the run recorded no cost, so this axis is unknown")
NO_MEMORY = deferred("the run recorded no worker memory")
NO_RUN = deferred("there is no run record to explain")


def run_axes(
    run: PipelineRun | None = None,
    *,
    directory: str | Path | None = None,
    reference: str | Path | None = None,
) -> dict:
    """The four axes of one run, from its record and its workspace.

    ``run`` is the persisted row -- its ``progress['cost']`` carries the speed
    primitives (RUN-01) and its ``options`` the fit facts. ``directory`` is the
    workspace accuracy and memory are read from, and ``reference`` an optional
    reference transcript for WER.

    A caller that has only a workspace passes ``directory`` and gets the two
    axes a workspace can prove, with reasons for the other two. This function
    never raises for a missing or old record: an axis without primitives is
    ``None`` with a reason.
    """
    cost = cost_of(run) if run is not None else {}
    return {
        "accuracy": _accuracy_axis(directory, reference),
        "speed": _speed_axis(cost),
        "memory": _memory_axis(directory),
        "fit": _fit_axis(run, cost),
    }


def _accuracy_axis(directory: str | Path | None, reference: str | Path | None) -> dict:
    """WER against a reference, else coverage and mean confidence.

    The numbers come from the CLI's one calibration implementation; an
    unreadable workspace or record is a reason, not a traceback.
    """
    axis: dict = {
        "basis": None,
        "wer": None,
        "similarity": None,
        "coverage": None,
        "mean_confidence": None,
        "words": None,
        "segments": None,
        "reason": None,
    }
    if directory is None:
        axis["reason"] = NO_WORKSPACE
        return axis
    try:
        report = stages.calibration_report(
            str(directory), str(reference) if reference else None
        )
    except (OSError, ValueError, KeyError, TypeError):
        axis["reason"] = NO_RECORD
        return axis
    axis.update(
        basis="wer" if "wer" in report else "coverage",
        wer=report.get("wer"),
        similarity=report.get("similarity"),
        coverage=report.get("coverage"),
        mean_confidence=report.get("mean_confidence"),
        words=report.get("words"),
        segments=report.get("segments"),
    )
    return axis


def _speed_axis(cost: dict) -> dict:
    """Audio seconds per wall second, presented as x-realtime.

    Both primitives are the run's own recorded measurement; the ratio is
    derived here and never stored. Either primitive missing (an old record, a
    run that never started) means the axis is unknown.
    """
    audio = number_or_none(cost.get("audio_seconds"))
    wall = number_or_none(cost.get("total_wall_seconds"))
    axis: dict = {
        "x_realtime": None,
        "audio_seconds": audio,
        "wall_seconds": wall,
        "reason": None,
    }
    if audio is None or wall is None or audio <= 0 or wall <= 0:
        axis["reason"] = NO_COST_RECORD
        return axis
    axis["x_realtime"] = round(audio / wall, 3)
    return axis


def _memory_axis(directory: str | Path | None) -> dict:
    """Peak RSS of the transcribe stage's decoder workers.

    The value is the one the stage measured and persisted; a stage that could
    not measure it wrote its reason instead, and this axis passes that reason
    through. A zero is never treated as a measurement.
    """
    axis: dict = {"peak_rss_bytes": None, "reason": None}
    if directory is None:
        axis["reason"] = NO_WORKSPACE
        return axis
    try:
        _, meta = Workspace.at(directory).load_segments()
    except (OSError, ValueError, KeyError, TypeError):
        meta = {}
    peak = int_or_none(meta.get("peak_rss_bytes"))
    if peak is not None and peak > 0:
        axis["peak_rss_bytes"] = peak
        return axis
    reason = meta.get("peak_rss_reason")
    axis["reason"] = reason if isinstance(reason, str) and reason else NO_MEMORY
    return axis


def _fit_axis(run: PipelineRun | None, cost: dict) -> dict:
    """What ``--auto`` chose, and the resolved facts the run worked with.

    ``chose`` and ``explanations`` are the CLI resolver's own recorded words;
    ``facts`` is the post-precedence profile and the cost record's backend,
    model, jobs, chunk size and machine description.
    """
    axis: dict = {"chose": [], "explanations": [], "facts": {}, "reason": None}
    if run is None:
        axis["reason"] = NO_RUN
        return axis
    meta = run.options if isinstance(run.options, dict) else {}
    auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
    backend_auto = (
        meta.get("backend_auto") if isinstance(meta.get("backend_auto"), dict) else {}
    )
    chose = auto.get("chose")
    axis["chose"] = (
        sorted(str(value) for value in chose) if isinstance(chose, list) else []
    )
    axis["explanations"] = [
        section["explanation"]
        for section in (backend_auto, auto)
        if isinstance(section.get("explanation"), str)
    ]
    knobs = meta.get("decoder_knobs")
    axis["facts"] = {
        "backend": run.backend,
        "model": run.model,
        "language": run.language,
        "profile": meta.get("profile"),
        "knobs": knobs if isinstance(knobs, dict) else {},
        "jobs": int_or_none(cost.get("jobs")),
        "chunk_seconds": number_or_none(cost.get("chunk_seconds")),
        "machine": cost.get("machine")
        if isinstance(cost.get("machine"), str)
        else None,
    }
    return axis


def render_axes(axes: dict) -> list[str]:
    """The terminal lines for a :func:`run_axes` value.

    This renders the service dict and nothing else -- no axis is recomputed
    here, so the CLI and the console cannot drift.
    """
    lines: list[str] = []
    accuracy = axes["accuracy"]
    if accuracy["basis"] == "wer":
        lines.append(
            tr(
                "[bench] accuracy: WER {wer} (similarity {similarity})",
                wer=_ratio(accuracy["wer"]),
                similarity=_ratio(accuracy["similarity"]),
            )
        )
    elif accuracy["basis"] == "coverage":
        lines.append(
            tr(
                "[bench] accuracy: coverage {coverage}, mean confidence "
                "{confidence}, {words} words",
                coverage=_ratio(accuracy["coverage"]),
                confidence=_ratio(accuracy["mean_confidence"]),
                words=_text(accuracy["words"]),
            )
        )
    else:
        lines.append(_unknown("accuracy", accuracy["reason"]))

    speed = axes["speed"]
    if speed["x_realtime"] is not None:
        lines.append(
            tr(
                "[bench] speed: {rate}x realtime ({audio}s audio / {wall}s wall)",
                rate=f"{speed['x_realtime']:.2f}",
                audio=f"{speed['audio_seconds']:.1f}",
                wall=f"{speed['wall_seconds']:.1f}",
            )
        )
    else:
        lines.append(_unknown("speed", speed["reason"]))

    memory = axes["memory"]
    if memory["peak_rss_bytes"] is not None:
        lines.append(
            tr(
                "[bench] memory: {mib} MiB peak across the transcribe stage's "
                "decoder workers",
                mib=f"{memory['peak_rss_bytes'] / (1 << 20):.1f}",
            )
        )
    else:
        lines.append(_unknown("memory", memory["reason"]))

    fit = axes["fit"]
    if fit["reason"]:
        lines.append(_unknown("fit", fit["reason"]))
        return lines
    lines.append(
        tr(
            "[bench] fit: --auto chose {chose}",
            chose=", ".join(fit["chose"]) or tr("nothing (chosen by hand)"),
        )
    )
    facts = fit["facts"]
    lines.append(
        tr(
            "[bench] fit facts: backend={backend} model={model} language={language} "
            "profile={profile} jobs={jobs} chunk={chunk}s",
            backend=_text(facts.get("backend")),
            model=_text(facts.get("model")),
            language=_text(facts.get("language")),
            profile=_text(facts.get("profile")),
            jobs=_text(facts.get("jobs")),
            chunk=_text(facts.get("chunk_seconds")),
        )
    )
    lines.extend(f"  {explanation}" for explanation in fit["explanations"])
    return lines


def _unknown(axis: str, reason: str | None) -> str:
    """One axis's unknown line, carrying the record's own reason."""
    return tr(
        "[bench] {axis}: unknown -- {reason}",
        axis=axis,
        reason=tr(reason) if reason else tr("no reason was recorded"),
    )


def _text(value: object) -> str:
    """A fact as terminal text (an unset fact is a dash, not the word None)."""
    return "-" if value is None else str(value)


def _ratio(value: float | None) -> str:
    """A fraction as terminal text (four decimals, or a dash when unknown)."""
    return "-" if value is None else f"{value:.4f}"


def run_bench(
    *,
    run_id: int | None = None,
    meeting_id: int | None = None,
    directory: str | None = None,
    reference: str | None = None,
    data_dir: str | None = None,
) -> int:
    """The ``bench`` handler: resolve what to measure and print the four axes."""
    if run_id is not None and meeting_id is not None:
        raise click.UsageError(tr("pass either --run-id or --meeting-id, not both"))
    if directory is not None and (run_id is not None or meeting_id is not None):
        raise click.UsageError(tr("pass --directory or a run id, not both"))
    if reference is not None and not Path(reference).is_file():
        raise click.BadParameter(
            tr("{path} is not a readable reference transcript", path=reference),
            param_hint="--reference",
        )

    run: PipelineRun | None = None
    workspace = directory
    if run_id is not None or meeting_id is not None:
        path = registry_path(data_dir)
        if not path.is_file():
            raise click.UsageError(
                tr("no registry at {path}: there is no run to measure", path=path)
            )
        registry = Registry.open(data_dir=data_dir)
        if run_id is not None:
            run = registry.get_run(run_id)
        else:
            meeting_runs = registry.list_runs(meeting_id)
            run = meeting_runs[0] if meeting_runs else None
        if run is None:
            raise click.UsageError(tr("no run found in the registry"))
        meeting = registry.meeting_by_id(run.meeting_id)
        workspace = meeting.workspace_path if meeting is not None else None
    elif workspace is None:
        workspace = "."

    for line in render_axes(run_axes(run, directory=workspace, reference=reference)):
        print(line)
    return 0


def register(group: click.Group) -> None:
    """Add the ``bench`` subcommand (called by the CLI's entry-point discovery).

    Registered through ``clear_record.commands`` (ADR-0013), so the CLI never
    imports this module directly and the terminal and the console render the
    same :func:`run_axes` value.
    """

    @group.command(
        name="bench",
        help=tr("show one run's four axes: accuracy, speed, memory, fit"),
    )
    @click.option("--run-id", type=int, default=None, help=tr("a run to measure"))
    @click.option(
        "--meeting-id",
        type=int,
        default=None,
        help=tr("the meeting whose latest run to measure"),
    )
    @click.option(
        "--directory",
        "-d",
        default=None,
        help=tr("a workspace to measure without a run record"),
    )
    @click.option(
        "--reference",
        default=None,
        help=tr("a reference transcript file to compare (WER/similarity)"),
    )
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help=tr(
            "override the app data directory (default: CR_DATA_DIR / platform dir)"
        ),
    )
    def _bench(**kwargs) -> int:
        return run_bench(**kwargs)


__all__ = [
    "NO_COST_RECORD",
    "NO_MEMORY",
    "NO_RECORD",
    "NO_RUN",
    "NO_WORKSPACE",
    "register",
    "render_axes",
    "run_axes",
    "run_bench",
]
