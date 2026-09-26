"""The four axes of a run -- the one place they are computed.

BENCH-01: everything a benchmark shows is derived, at display time, from the run
record, on four axes and no fifth:

* **accuracy** -- WER against a reference transcript when one is configured,
  otherwise coverage and mean confidence. The arithmetic is the pipeline's
  (:func:`clear_record.pipeline.stages.calibration_report`), reused verbatim, so the
  terminal report and the console cannot disagree.
* **speed** -- audio seconds over total wall seconds, presented as x-realtime.
  Both primitives come from the run's cost record (RUN-01); the ratio itself is
  never stored.
* **memory** -- the peak RSS of the transcribe stage's decoder workers, measured
  while they ran (see :mod:`clear_record.pipeline.transcription`). A platform that
  cannot measure it reports ``None`` with a reason, never zero.
* **fit** -- what ``--auto`` chose and the facts it had to work with, read back
  from the run meta the resolvers recorded.

Every axis is a dict, and an axis whose primitives are missing carries a
``reason`` (a message ID; render it with ``tr``) instead of a number. A run
recorded before the cost record existed, a run that failed before its first
stage, or no run at all yields the same four axes -- never an exception.

Layering: this is a ``service`` module, so it may import ``pipeline`` (it reuses
the pipeline's calibration arithmetic and the workspace reader). The ``bench``
subcommand is registered through the ``clear_record.commands`` entry point, so
``clear_record.cli`` never imports this module (ADR-0013) and the terminal and
the console render the *same* dict.
"""

from __future__ import annotations

from pathlib import Path

import click

from clear_record.pipeline import stages
from clear_record.pipeline.workspace import Workspace
from clear_record.core.i18n import deferred, tr
from clear_record.core.paths import registry_path
from clear_record.service.models import PipelineRun
from clear_record.service.runs import cost_of, int_or_none, number_or_none
from clear_record.service.store import Registry

#: Why an axis has no number. These are message IDs (:func:`deferred`): each
#: surface translates them with ``tr`` at its own boundary.
NO_WORKSPACE = deferred("the run has no workspace to measure")
NO_RECORD = deferred("the workspace has no readable record to score")
NO_COST_RECORD = deferred("the run recorded no cost, so this axis is unknown")
NO_MEMORY = deferred("the run recorded no worker memory")
NO_RUN = deferred("there is no run record, so this axis is unknown")


def run_axes(
    run: PipelineRun | None = None,
    *,
    directory: str | Path | None = None,
    reference: str | Path | None = None,
) -> dict:
    """The four axes of one run, from its record and the run's own copy.

    ``run`` is the persisted row -- its ``progress['cost']`` carries the speed
    primitives (RUN-01) and its ``options`` the fit facts. ``directory`` is the
    workspace the run belongs to: the accuracy and memory axes are read from
    **that run's own copy** under it (``<workspace>/runs/<run id>/``, ADR-0033),
    because the workspace root holds only the newest *published* copy — a run
    that failed, stopped or was interrupted published nothing and must report its
    own reason rather than a previous run's numbers. ``reference`` is an optional
    reference transcript for WER.

    A caller that has only a workspace passes ``directory`` with no run and gets
    the two axes a workspace can prove, with reasons for the other two. This
    function never raises for a missing or old record: an axis without
    primitives is ``None`` with a reason.
    """
    cost = cost_of(run) if run is not None else {}
    subject = _axes_directory(run, directory)
    return {
        "accuracy": _accuracy_axis(subject, reference),
        "speed": _speed_axis(cost, has_run=run is not None),
        "memory": _memory_axis(subject, cost if run is not None else None),
        "fit": _fit_axis(run, cost),
    }


def _axes_directory(
    run: PipelineRun | None, directory: str | Path | None
) -> str | Path | None:
    """The directory a run's axes are read from: its own copy, when there is one.

    A run writes its documents into its own scope (``<workspace>/runs/<run
    id>/``) and only a **finished** run publishes them at the workspace root
    (ADR-0033). Reading the root for a run that published nothing would show the
    previous finished run's record as this run's; with no run the workspace
    itself is the subject.
    """
    if run is None or directory is None:
        return directory
    return Workspace.at(directory).run_scope(run.id).outputs


def _accuracy_axis(directory: str | Path | None, reference: str | Path | None) -> dict:
    """WER against a reference, else coverage and mean confidence.

    The numbers come from the pipeline's one calibration implementation; an
    unreadable copy — the run's own, or the workspace's when there is no run — is
    a reason, not a traceback.
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
    # AttributeError is in the tuple on purpose: a readable but malformed
    # document (a list, a bare string) is a reason, not a 500 on the tab.
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
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


def _speed_axis(cost: dict, *, has_run: bool) -> dict:
    """Audio seconds per wall second, presented as x-realtime.

    Both primitives are the run's own recorded measurement; the ratio is
    derived here and never stored. Either primitive missing (an old record, a
    run that never started) means the axis is unknown -- and with no run at
    all the reason says so instead of blaming a missing cost record.
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
        axis["reason"] = NO_COST_RECORD if has_run else NO_RUN
        return axis
    axis["x_realtime"] = round(audio / wall, 3)
    return axis


def _memory_axis(directory: str | Path | None, cost: dict | None) -> dict:
    """Peak RSS of the transcribe stage's decoder workers.

    The measurement is **run-scoped**. With a run (``cost`` is that run's cost
    record, empty or not) both the value and the reason come from the record,
    because ``segments.json`` belongs to whichever run wrote it last: a failed
    run must not show a previous run's peak, and a later all-reused re-run
    must not erase an earlier run's measurement. Only a caller with no run
    (``cost is None``) reads ``directory`` at all, and it is then the workspace
    the caller named; with a run, :func:`run_axes` hands this function the run's
    own copy (``_axes_directory``) and nothing here reads it. A zero is never a
    measurement on either path.
    """
    axis: dict = {"peak_rss_bytes": None, "reason": None}
    if cost is not None:
        peak = int_or_none(cost.get("peak_rss_bytes"))
        if peak is not None and peak > 0:
            axis["peak_rss_bytes"] = peak
            return axis
        reason = cost.get("peak_rss_reason")
        axis["reason"] = reason if isinstance(reason, str) and reason else NO_MEMORY
        return axis
    if directory is None:
        axis["reason"] = NO_WORKSPACE
        return axis
    try:
        _, meta = Workspace.at(directory).load_segments()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
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

    ``chose`` and ``explanations`` are the pipeline resolver's own recorded words;
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


# --- display formatting ---------------------------------------------------- #
#
# One implementation per rendered figure, shared by the terminal renderer and
# the console template: a null figure is the same dash in both, and a number
# is rounded the same way. Neither surface formats an axis value itself.


def format_ratio(value: float | None) -> str:
    """A fraction for display: four decimals, or a dash when unknown."""
    return "-" if value is None else f"{value:.4f}"


def format_rate(value: float | None) -> str:
    """An x-realtime rate for display: two decimals, or a dash when unknown."""
    return "-" if value is None else f"{value:.2f}"


def format_seconds(value: float | None) -> str:
    """Seconds for display: one decimal, or a dash when unknown."""
    return "-" if value is None else f"{value:.1f}"


def format_mib(value: int | None) -> str:
    """Bytes as mebibytes for display: one decimal, or a dash when unknown.

    The bytes-to-MiB conversion lives here, not in a template: it is a display
    unit, and the axis carries bytes because that is what was measured.
    """
    return "-" if value is None else f"{value / (1 << 20):.1f}"


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
                wer=format_ratio(accuracy["wer"]),
                similarity=format_ratio(accuracy["similarity"]),
            )
        )
    elif accuracy["basis"] == "coverage":
        lines.append(
            tr(
                "[bench] accuracy: coverage {coverage}, mean confidence "
                "{confidence}, {words} words",
                coverage=format_ratio(accuracy["coverage"]),
                confidence=format_ratio(accuracy["mean_confidence"]),
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
                rate=format_rate(speed["x_realtime"]),
                audio=format_seconds(speed["audio_seconds"]),
                wall=format_seconds(speed["wall_seconds"]),
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
                mib=format_mib(memory["peak_rss_bytes"]),
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
            "profile={profile} jobs={jobs} chunk={chunk}",
            backend=_text(facts.get("backend")),
            model=_text(facts.get("model")),
            language=_text(facts.get("language")),
            profile=_text(facts.get("profile")),
            jobs=_text(facts.get("jobs")),
            chunk=_chunk_text(facts.get("chunk_seconds")),
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


def _chunk_text(value: object) -> str:
    """A chunk size as terminal text with its unit, or a bare dash."""
    seconds = number_or_none(value)
    return "-" if seconds is None else f"{seconds:.1f}s"


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
    "format_mib",
    "format_rate",
    "format_ratio",
    "format_seconds",
    "register",
    "render_axes",
    "run_axes",
    "run_bench",
]
