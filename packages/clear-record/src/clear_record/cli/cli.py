"""The `clear-record` CLI.

One subcommand per pipeline stage (plus `run`/`calibrate` conveniences). The
subcommand surface is derived from the pipeline spec so the CLI and domain cannot
drift; the heavy per-stage logic lives in :mod:`clear_record.pipeline.stages`.

The parser is **Click** (ADR-0022). The two properties the port must keep are:

- the subcommand surface is **derived from** :func:`pipeline_spec` — the built-in
  stage commands are added in a loop over ``pipeline_spec().stages``, exactly as
  the ``run`` dispatch is, so the CLI and the domain still read one declaration;
- the **stage commands'** default stdout is byte-identical: the stages print
  nothing and this module renders every word of what they returned and reported
  — ``run``'s is the node's own stream, read back (below) — pinned command by
  command in ``tests/cli/test_stage_stdout.py``.

`run` is the one command that does not run a stage here: a run started from the
command line is a **node run** (ADR-0032), so it names its workspace in one
request to the recorded node and follows the row the node owns
(:mod:`clear_record.cli.runs`). The stage commands and `calibrate` keep the
in-process path above, which is why the byte pin still covers them.

A run knob — its flag spelling, its ``CR_*`` binding, its type and its ``--help``
text — is declared once, in ``core.options.RUN_KNOBS``; the options for
``transcribe``/``run``/``calibrate`` are generated from those rows. Each generated
option still carries ``envvar=``, so the name stays discoverable in ``--help``,
and the resolution order — flag > ``CR_*`` environment > profile > built-in
default — lives in one mechanism (:func:`clear_record.core.resolve_options`).
That chain is a run knob's own, not ADR-0007's app-directory precedence, which
has a config-file layer and no profile.
The ``CR_*`` reads that live outside the CLI (``providers``, ``pipeline``,
``service``, ``web``, ``tray``, ``mcp``, ``core.diagnostics`` and
``core.i18n``'s ``CR_LANG``) are deliberately left where they are.

Recordings and model weights are environment-local data — never commit them.
See docs/architecture.md §6 and ADR-0006.
"""

from __future__ import annotations

import dataclasses
import functools
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, NoReturn, Sequence, TypeVar

import click

from clear_record.core import (
    DECODER_KNOBS,
    PROFILE_CUSTOM,
    PROFILES,
    RUN_KNOBS,
    Alignment,
    JobEvent,
    PipelineOptions,
    RecordDocument,
    ScopeError,
    Segment,
    Step,
    log_event,
    node,
    parse_time_range,
    pipeline_spec,
    resolve_options,
    set_level,
)
from clear_record.core.i18n import (
    available_locales,
    deferred,
    install as install_language,
    resolve_locale,
    tr,
)
from clear_record.providers import (
    BACKENDS,
    available_backend_ids,
    backend_availability,
    resolve_models_dir,
)

from clear_record.cli.calibrate import calibrate_report
from clear_record.cli.runs import CLI_ORIGIN, Refused
from clear_record.cli.runs import follow as follow_run
from clear_record.cli.runs import start as start_run
from clear_record.cli.synth import synth
from clear_record.pipeline import auto
from clear_record.pipeline import stages

# The preview's timestamp format is the pipeline's own (``format_timestamp``): the
# Markdown serializer renders through it too, while the SRT/VTT renderers go
# through ``_srt_tc``, so a copy here would be a second format to keep in step.
from clear_record.pipeline.stages import format_timestamp
from clear_record.pipeline.workspace import Workspace

#: Entry-point group for optional subcommand providers. The bundled web console
#: registers here (ADR-0013) so this module never imports it, keeping the
#: dependency arrow acyclic and a plain CLI run cheap.
COMMAND_ENTRY_POINT_GROUP = "clear_record.commands"

#: The command name stays the owner's spelling (ADR-0009, ADR-0022).
_PROG = "clear-record"
# ``deferred`` marks a message ID whose lookup happens at group-build time (Click
# snapshots help then); it returns the English string, and ``_build_group``
# passes the result through ``tr`` once the catalog is installed.
_DESCRIPTION = deferred("clear-record: from many recordings to one clear record.")

_VERBOSE_HELP = deferred(
    "raise diagnostics log detail (the flag form of CR_LOG_LEVEL=debug); the "
    "log is written to the app state directory, never to stdout"
)


def _lang_help() -> str:
    """The ``--lang`` help, naming the locales that actually ship a catalog."""
    shipped = available_locales()
    extra = f" (shipped: {', '.join(shipped)})" if shipped else ""
    return (
        "UI language: a language tag like `de` or `fr_FR`. Precedence is "
        "--lang > CR_LANG > LC_ALL/LANG > English"
        f"{extra}; English renders the source strings verbatim."
    )


def _install_language(ctx: click.Context, param: click.Parameter, value: str | None):
    """Eager callback: install the catalog before the command body runs.

    ``--lang`` is also read in :func:`main` before the group is built so Click's
    help text (snapshotted at build time) is translated too; this callback keeps
    a directly-invoked group honest for runtime messages.
    """
    install_language(resolve_locale(value))
    return value


def _requested_lang(argv: Sequence[str] | None) -> str | None:
    """Pull ``--lang``/``--lang=`` out of the raw argv.

    Click parses options only after the command objects exist, but Click's help
    strings are captured when the group is built. :func:`main` therefore reads
    the flag once up front so ``clear-record --lang de --help`` is translated;
    Click still owns validation and the flag's precedence (see the option).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, token in enumerate(args):
        if token == "--lang" and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--lang="):
            return token.split("=", 1)[1]
    return None


#: The ASR set offered by ``--backend``: the provider catalog plus the
#: capability-driven ``auto`` sentinel. Read from the catalog so a new backend is
#: offered with no CLI edit.
_DEFAULT_BACKEND = next(iter(BACKENDS))


def _external_commands():
    """Installed subcommand providers, ordered by entry-point name.

    Imported lazily so the base CLI carries no dependency on any provider.
    """
    from importlib.metadata import entry_points

    return sorted(entry_points(group=COMMAND_ENTRY_POINT_GROUP), key=lambda ep: ep.name)


def _register_external_subcommands(group: click.Group) -> None:
    """Let each installed provider contribute its Click command to the group.

    A provider that cannot be imported is **skipped, not fatal**. Entry points
    are declared by the distribution whether or not the extra supplying their
    dependencies is installed, and a frozen bundle may not carry a provider
    module at all, so a failed import must never take the whole CLI down --
    that is what made the packaged macOS app die on launch with
    "ModuleNotFoundError: clear_record.mcp". The provider command itself is
    what names the missing extra.
    """
    for entry_point in _external_commands():
        try:
            register = entry_point.load()
        except ImportError:
            continue
        register(group)


# --------------------------------------------------------------------------- #
# verbosity: `-v` before *or* after the subcommand
# --------------------------------------------------------------------------- #
def _verbose_flag(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """Eager callback for the per-subcommand ``-v``: raise detail when present.

    Absence is handled once by :meth:`_Group.invoke`, which clears the level for
    the group-level flag, so a repeated programmatic call cannot leak ``debug``.
    """
    if value:
        set_level("debug")
    return value


def _ensure_verbose(command: click.Command) -> None:
    """Append ``-v``/``--verbose`` to a command that does not already have it.

    ``-v`` is accepted on every subcommand as well as on the group, so
    ``clear-record -v run …`` and ``clear-record run … -v`` both raise detail;
    the option is ``expose_value=False`` because the callback sets the level and
    the command body has no use for the flag.
    """
    if any(param.name == "verbose" for param in command.params):
        return
    command.params.append(
        click.Option(
            ("-v", "--verbose"),
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=_verbose_flag,
            help=tr(_VERBOSE_HELP),
        )
    )


class _Group(click.Group):
    """A group that owns the CLI-wide verbosity contract.

    ``invoke`` runs after the group's own options are parsed but before the
    subcommand's are, so it normalizes the level from the group flag and any
    subcommand ``-v`` still gets the last word. ``add_command`` gives every
    command — built-in or contributed through an entry point — the same ``-v``.
    """

    def invoke(self, ctx: click.Context) -> Any:
        set_level("debug" if ctx.params.get("verbose") else None)
        return super().invoke(ctx)

    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        _ensure_verbose(cmd)
        super().add_command(cmd, name=name)


# --------------------------------------------------------------------------- #
# option vocabulary
# --------------------------------------------------------------------------- #
def _with_options(*decorators):
    """Compose Click option/argument decorators, preserving their order."""

    def apply(fn):
        for decorator in reversed(decorators):
            fn = decorator(fn)
        return fn

    return apply


def _split_value(split_channels: bool, mix_down: bool) -> str:
    """Map the `--split-channels`/`--mix-down` flags to the stage's value.

    Neither flag is the `auto` default. Both together is a usage error, not a
    silent last-wins: contradictory flags must fail loudly.
    """
    if split_channels and mix_down:
        raise click.UsageError(
            tr(
                "--split-channels and --mix-down are mutually exclusive; "
                "pass one or neither."
            )
        )
    if split_channels:
        return "split"
    if mix_down:
        return "mix"
    return "auto"


def _do_diarize(diarize: bool, no_diarize: bool) -> bool | None:
    """Map `--diarize`/`--no-diarize` to the tri-state (``None`` = choose)."""
    if diarize and no_diarize:
        raise click.UsageError(
            tr(
                "--diarize and --no-diarize are mutually exclusive; pass one or neither."
            )
        )
    if diarize:
        return True
    if no_diarize:
        return False
    return None


def _validate_directory(ctx: click.Context, param: click.Parameter, value: str | None):
    """Reject a file where a directory is expected, before any stage runs.

    A novice's first instinct is `clear-record run meeting.m4a`; without this the
    command walks into the workspace code and fails with an unrelated ingest
    error. A path that does not exist yet is fine (`synth` creates it).
    """
    if value and Path(value).is_file():
        raise click.BadParameter(
            tr(
                "{path} is a file, not a directory: this command expects a "
                "directory of audio files.",
                path=value,
            ),
            ctx=ctx,
            param=param,
        )
    return value


def _rerun_range(ctx: click.Context, param: click.Parameter, value: str | None):
    """Validate ``--rerun-range`` at parse time, so a typo is a usage error.

    Validating here (rather than in the stage) makes a malformed range fail
    before any backend is resolved or any chunk planned — and it can never be
    silently dropped into an unscoped full re-decode.
    """
    if not value:
        return None
    try:
        parse_time_range(value)
    except ScopeError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc
    return value


_DIRECTORY = _with_options(click.argument("directory", callback=_validate_directory))
_PATHS = _with_options(
    click.argument("directory", callback=_validate_directory),
    click.argument("inputs", nargs=-1),
)
_REFERENCE = _with_options(
    click.option(
        "--reference",
        default=None,
        help="reference source id for alignment (default: first)",
    )
)
# Two separate flags (not a `--x/--y` pair) so the callback can reject both
# being given: a Click pair would silently take the last one.
_CHANNEL = _with_options(
    click.option(
        "--split-channels",
        "split_channels",
        is_flag=True,
        help="split every channel of a multichannel file into its own source",
    ),
    click.option(
        "--mix-down",
        "mix_down",
        is_flag=True,
        help="always downmix multichannel audio to mono",
    ),
)
_DIARIZE = _with_options(
    click.option(
        "--diarize",
        "diarize",
        is_flag=True,
        help="force multi-speaker diarization",
    ),
    click.option(
        "--no-diarize",
        "no_diarize",
        is_flag=True,
        help="disable diarization",
    ),
    click.option(
        "--speakers",
        type=int,
        default=None,
        help="known number of speakers (default: estimate from the audio)",
    ),
)
# The standalone `diarize` command always diarizes, so it exposes only the knob
# it acts on: a `--no-diarize` there would be accepted and then ignored.
_DIARIZE_ONLY = _with_options(
    click.option(
        "--speakers",
        type=int,
        default=None,
        help="known number of speakers (default: estimate from the audio)",
    )
)
_ATTRIBUTE = _with_options(
    click.option(
        "--attribute-energy",
        is_flag=True,
        help="attribute speakers by relative source energy (close-mic cross-talk) "
        "instead of spectral diarization",
    ),
    click.option(
        "--mixed-source",
        default=None,
        help="manifest source id to use as the mixed/room reference for energy "
        "attribution",
    ),
    click.option(
        "--window-s",
        type=float,
        default=None,
        help="seconds of causal history for a rolling per-source level (tracks "
        "drifting gain); omit for the static whole-recording level",
    ),
)
# The standalone `attribute` command exposes only the two knobs it acts on.
_ATTRIBUTE_ONLY = _with_options(
    click.option(
        "--mixed-source",
        default=None,
        help="manifest source id to use as the mixed/room reference",
    ),
    click.option(
        "--window-s",
        type=float,
        default=None,
        help="seconds of causal history for a rolling per-source level (tracks "
        "drifting gain); omit for the static whole-recording level",
    ),
)
_ADD = _with_options(
    click.option(
        "--add",
        multiple=True,
        help="term(s)/phrase(s) to append; repeat the flag, e.g. --add A --add B",
    )
)
_SYNTH = _with_options(
    click.option(
        "--devices", type=int, default=4, help="number of recording devices (default 4)"
    ),
    click.option(
        "--duration",
        type=float,
        default=20.0,
        help="scene duration in seconds (default 20)",
    ),
    click.option(
        "--speakers",
        type=int,
        default=4,
        help="number of speakers in the scene (default 4)",
    ),
    click.option("--seed", type=int, default=0, help="random seed"),
)
_BACKEND_ALL = _with_options(
    click.option(
        "--all",
        "all_backends",
        is_flag=True,
        help="list all known backends, not only available ones",
    )
)
_REFERENCE_TRANSCRIPT = _with_options(
    click.option(
        "--reference-transcript",
        "reference_transcript",
        default=None,
        help="a reference transcript text file to compare (WER/similarity)",
    )
)


def _knob_options(slot: str) -> list:
    """The Click options for one slot of the run-knob table (``core.RUN_KNOBS``).

    Each generated option states itself once, from its row: the spelling, the
    type, the ``CR_*`` binding and the help. The default is always ``None``
    ("unset"), never the concrete built-in value, so an explicit flag that equals
    the default — ``--jobs 0``, the documented "auto" — is still recognized as
    explicit and beats a profile or the environment (see
    :func:`clear_record.core.resolve_options`).
    """
    return [
        click.option(
            *knob.cli,
            knob.name,
            type=knob.convert,
            default=None,
            envvar=knob.env,
            show_envvar=True,
            help=knob.help,
        )
        for knob in RUN_KNOBS
        if knob.cli_slot == slot
    ]


#: The backend/decoder subset shared by `transcribe`, `run` and `calibrate`. Each
#: option mirrors its ``CR_*`` variable; ``--profile`` stays ``None`` when unset
#: (a real sentinel, like the other resolver-managed knobs), so `--auto` can tell
#: "choose for me" from an explicit `--profile custom`.
_BACKEND = _with_options(
    click.option(
        "--backend",
        "-b",
        type=click.Choice((*BACKENDS, auto.BACKEND_AUTO)),
        default=_DEFAULT_BACKEND,
        help=f"ASR backend (default {_DEFAULT_BACKEND}); "
        f"{auto.BACKEND_AUTO!r} picks the best available backend "
        "(native first, whisper-cli fallback)",
    ),
    click.option(
        "--auto",
        is_flag=True,
        help="recommended default: inspect the machine and the tape, choose "
        "a profile and model, and explain the choice (never downloads a "
        "model; explicit flags still win)",
    ),
    click.option(
        "--model",
        "-m",
        default=None,
        help="model checkpoint name/size, e.g. tiny/base/small/medium",
    ),
    click.option(
        "--language", "-l", default=None, help="language hint for ASR (default auto)"
    ),
    click.option(
        "--models-dir",
        default=resolve_models_dir,
        envvar="CR_MODELS_DIR",
        show_envvar=True,
        help="model download dir (default: models/ under the platform data dir)",
    ),
    click.option(
        "--glossary",
        default=None,
        help="glossary file (one term/line) used as the ASR initial prompt; "
        "defaults to <directory>/glossary.txt if present",
    ),
    # The knobs of `core.RUN_KNOBS`, spliced in at their declared slots: the
    # chunking pair here, then `--jobs` after `--no-resume`, then the decoder
    # knobs last. Each carries its own spelling, `CR_*` var, type and help.
    *_knob_options("chunking"),
    click.option(
        "--no-resume",
        "resume",
        is_flag=True,
        flag_value=False,
        default=True,
        help="ignore cached chunks and re-transcribe from scratch",
    ),
    *_knob_options("sizing"),
    click.option(
        "--check-plugin",
        is_flag=True,
        help="one-shot whisper-cli load probe to confirm the ggml GPU plugin "
        "actually loads before transcribing (opt-in; the default probe only "
        "checks that the plugin file is present)",
    ),
    click.option(
        "--profile",
        type=click.Choice(tuple(PROFILES)),
        default=None,
        help="a preset trading decoder effort at a fixed model; any explicit "
        "flag overrides it (default custom = set nothing). None (unset) "
        "lets --auto choose the profile",
    ),
    click.option(
        "--rerun-source",
        "rerun_sources",
        multiple=True,
        help="re-run scope: re-decode only this source's chunks, reusing every "
        "other chunk from the cache; repeat for several sources (default: every "
        "source)",
    ),
    click.option(
        "--rerun-range",
        "rerun_range",
        default=None,
        callback=_rerun_range,
        help="re-run scope: re-decode only the chunks overlapping START-END "
        "(H:MM or H:MM:SS, or seconds; e.g. 12:30-18:00); chunks outside keep "
        "their cached decode",
    ),
    *_knob_options("decoder"),
)


# --------------------------------------------------------------------------- #
# option resolution (shared by the backend-bearing commands)
# --------------------------------------------------------------------------- #
def _backend_option_kwargs(args: Any) -> dict:
    """The backend/decoder subset shared by `transcribe`, `run` and `calibrate`."""
    return {
        "backend": args.backend,
        "model": args.model,
        "language": args.language,
        "model_dir": args.models_dir,
        "glossary": args.glossary,
        "resume": args.resume,
        "check_plugin": args.check_plugin,
        "rerun_sources": tuple(args.rerun_sources) or None,
        "rerun_range": args.rerun_range,
        # The parser leaves `--profile` as `None` when unset (a real sentinel,
        # like the other resolver-managed knobs), so `--auto` can tell "choose
        # for me" from an explicit `--profile custom`.
        "profile": args.profile or PROFILE_CUSTOM,
        # Every knob the table declares is read off the parsed arguments by its
        # own name, so a new row needs no edit here.
        **{knob.name: getattr(args, knob.name) for knob in RUN_KNOBS},
    }


def _apply_auto(args: Any, options: PipelineOptions) -> PipelineOptions:
    """Resolve ``--backend auto`` and ``--auto`` on top of the explicit options.

    ``--backend auto`` is a capability choice, separate from the profile: it is
    replaced by the best *available* backend (printed for the operator).
    ``--auto`` fills a profile + model only where the user left them unset, and
    refuses to run when the recommended model is absent rather than downloading
    it. An explicit ``--backend``/``--profile``/``--model`` still wins.
    """
    if args.backend == auto.BACKEND_AUTO:
        try:
            backend_choice = auto.resolve_backend(available_backend_ids())
        except auto.NoBackendAvailable as exc:
            log_event("error", "cli", "cli.auto.failed", reason=str(exc))
            raise SystemExit(exc.message.render(tr)) from exc
        print(backend_choice.message.render(tr))
        options = dataclasses.replace(options, backend=backend_choice.backend)

    if not args.auto:
        return resolve_options(options, profile=args.profile)

    choice = auto.resolve_auto(
        auto.probe_auto(
            args.directory,
            model_dir=args.models_dir,
            language=args.language,
        )
    )
    print(choice.message.render(tr))

    if options.model is None:
        if not choice.model_on_disk:
            log_event(
                "error",
                "cli",
                "cli.auto.failed",
                reason=f"recommended model {choice.model!r} is not on disk",
            )
            raise SystemExit(
                tr(
                    "[auto] the recommended model {model!r} is not in the "
                    "models directory, and --auto never downloads one.\n"
                    "  Pre-fetch it, e.g. `hf download ggerganov/whisper.cpp "
                    "ggml-{model}.bin --local-dir {models_dir}`, or pass "
                    "an explicit `--model`.",
                    model=choice.model,
                    models_dir=args.models_dir,
                )
            )
        options = dataclasses.replace(options, model=choice.model)

    if choice.diarize and options.do_diarize is None:
        options = dataclasses.replace(options, do_diarize=True)

    # `--auto` supplies the profile only when the user did not name one; the
    # environment still beats it, and every explicit knob beats both.
    profile = args.profile if args.profile is not None else choice.profile
    return resolve_options(options, profile=profile)


def _pipeline_options(args: Any) -> PipelineOptions:
    """Fill the one run-options value from the parsed CLI arguments and resolve it.

    The environment and the profile fill any knob the user left at its default;
    an explicit flag wins (see :func:`clear_record.core.resolve_options`), and
    the opt-in ``--auto`` / ``--backend auto`` resolvers fill in last.
    """
    kwargs = _backend_option_kwargs(args)
    kwargs.update(
        split=_split_value(
            getattr(args, "split_channels", False),
            getattr(args, "mix_down", False),
        ),
        do_diarize=_do_diarize(
            getattr(args, "diarize", False),
            getattr(args, "no_diarize", False),
        ),
        speakers=getattr(args, "speakers", None),
        reference=getattr(args, "reference", None),
        attribute_energy=getattr(args, "attribute_energy", False),
        mixed_source=getattr(args, "mixed_source", None),
        window_s=getattr(args, "window_s", None),
    )
    return _apply_auto(args, PipelineOptions(**kwargs))


def _options(kwargs: dict) -> PipelineOptions:
    """Resolve the run options from one command's Click parameters."""
    return _pipeline_options(SimpleNamespace(**kwargs))


# --------------------------------------------------------------------------- #
# rendering what a stage produced
# --------------------------------------------------------------------------- #
# The pipeline returns what a stage produced and reports its own mid-stage lines
# through the sink it is handed; how either reads is this surface's business.
# Each renderer takes the value the stage returned (and the workspace, for the
# artifact path a command names) and writes the bytes the stage used to print for
# itself — one renderer per stage, so a single stage command and `run`'s blocks
# cannot drift apart.


class _StageLines:
    """This surface's sink on a stage: print the stage's own lines as they arrive.

    A stage's mid-stage lines are reported on its sink — one structured
    :class:`~clear_record.core.JobEvent` per line, the same payload the console's
    run stream already listens to — and the line's text is additionally offered
    to the sink's optional ``line`` capability (the seam
    :func:`~clear_record.pipeline.workspace.report_line` reads it through).
    Progress events are for a bar this surface does not draw, so it consumes
    none of them.
    """

    def __call__(self, _event: JobEvent) -> None:
        """A progress report: nothing for this surface to render."""

    def line(self, text: str) -> None:
        """One of the stage's own lines, printed where the stage produced it."""
        print(text)


def _render_ingest(report: stages.IngestReport, workspace: Workspace) -> None:
    # The per-input decode lines are the stage's own, reported on the sink as
    # each decode begins (``_StageLines``); what is left here is the summary and
    # the source list, which are about the pass as a whole.
    print(f"[ingest] {len(report.sources)} source(s) -> {workspace.manifest_path}")
    for source in report.sources:
        print(f"  {source.id:24s} {source.path}")


def _render_align(alignment: Alignment, _workspace: Workspace) -> None:
    print(
        f"[align] reference={alignment.reference} method={alignment.method} "
        f"conf={alignment.confidence} unresolved={len(alignment.unresolved)}"
    )
    for sid, offset in alignment.offsets.items():
        marker = " (ref)" if sid == alignment.reference else ""
        print(f"  {sid:24s} offset={offset:+.4f}s{marker}")
    for sid in alignment.unresolved:
        print(f"  {sid:24s} UNRESOLVED (could not place this source)")


def _render_transcribe(report: stages.TranscribeReport, workspace: Workspace) -> None:
    print(
        f"[transcribe] {report.meta.get('model')!r} via "
        f"{report.meta.get('backend')} -> segments.json"
    )
    for sid, segs in report.per_source.items():
        info = report.meta.get("sources", {}).get(sid, {})
        duration = info.get("duration")
        print(
            f"  {sid:24s} segments={len(segs):4d}  "
            f"duration={duration if duration is not None else '?'}  "
            f"chunks={info.get('chunks', '?')}"
        )
    if report.attribution is not None:
        # The attribution pass is this stage's own (`--attribute-energy` is an
        # alternative to diarization, not a declared stage), so its line follows
        # the summary the stage that ran it renders.
        _render_attribute(report.attribution, workspace)


def _render_attribute(report: stages.AttributeReport, _workspace: Workspace) -> None:
    if not report.segments:
        print("[attribute] no segments to attribute")
        return
    suffix = f" (room reference: {report.mixed_source})" if report.mixed_source else ""
    if report.window_s is not None:
        suffix += f" (rolling window: {report.window_s:g}s)"
    print(
        f"[attribute] {report.segments} segment(s), {report.speakers} speaker(s), "
        f"{report.changed} re-attributed{suffix}"
    )


def _render_glossary(report: stages.GlossaryReport, _workspace: Workspace) -> None:
    print(f"[glossary] {report.path} ({len(report.terms)} term(s))")
    for term in report.terms:
        print(f"  {term}")


def _render_reconcile(record: RecordDocument, workspace: Workspace) -> None:
    speakers = {seg.speaker for seg in record.segments}
    print(
        f"[reconcile] {len(record.segments)} segment(s), {len(speakers)} attributed "
        f"speaker(s) -> {workspace.record_path}"
    )
    _render_transcript_preview(record.segments)


def _render_transcript_preview(segments: Sequence[Segment], limit: int = 12) -> None:
    for seg in segments[:limit]:
        print(
            f"  {format_timestamp(seg.start)} [{seg.speaker or seg.source}] {seg.text}"
        )
    if len(segments) > limit:
        print(f"  … {len(segments) - limit} more")


def _render_export(written: dict[str, Path], _workspace: Workspace) -> None:
    for fmt, path in written.items():
        print(f"[export] {fmt:4s} -> {path}")


#: One renderer per declared stage: the single-stage commands call these
#: directly, and ``calibrate``'s run renders each stage through the same table.
#: ``run`` does not: its stages run on the node, which streams their progress
#: rather than their words (see ``clear_record.cli.runs``).
_RENDERERS: dict[Step, Callable[[Any, Workspace], None]] = {
    Step.INGEST: _render_ingest,
    Step.ALIGN: _render_align,
    Step.TRANSCRIBE: _render_transcribe,
    Step.RECONCILE: _render_reconcile,
    Step.EXPORT: _render_export,
}


def _render_stage(workspace: Workspace, step: Step, result: Any) -> None:
    """Render one stage of a `run`, where that stage ran (see ``stages.run``)."""
    _RENDERERS[step](result, workspace)


def _run_pipeline(directory: str, options: PipelineOptions) -> None:
    """Run the whole pipeline in this process, rendering each stage as it lands.

    ``run`` does **not** come through here any more: a command-line run is the
    node's run (ADR-0032, :func:`_cmd_run`). What is left is ``calibrate``, whose
    subject is this machine's own measurement of a workspace — it runs the
    pipeline and then reports the numbers it reads from that workspace.

    ``on_result`` is what keeps the run's stdout in one order: a stage's own lines
    are printed through the sink while it works, so its summary has to be
    rendered the moment that stage finishes rather than after the last one.
    """
    workspace = Workspace.at(directory)
    stages.run(
        directory,
        options,
        on_event=_StageLines(),
        on_result=functools.partial(_render_stage, workspace),
    )


# --------------------------------------------------------------------------- #
# command bodies
# --------------------------------------------------------------------------- #
def _cmd_backends(**kwargs: Any) -> int:
    for bid, status in backend_availability().items():
        if not kwargs["all_backends"] and not status.available:
            continue
        state = "available" if status.available else "unavailable"
        reason = f" — {str(status.reason)}" if status.reason else ""
        print(f"{bid:12s} {state}{reason}")
    return 0


# --------------------------------------------------------------------------- #
# the node: ensure one is up before the work a command does needs it
# --------------------------------------------------------------------------- #
#: How long a node a command started gets to answer before the command calls the
#: machine one where no node can start. A node binds and records its address in
#: well under a second, so this is room for a slow machine, not a wait anyone
#: should feel.
_NODE_START_TIMEOUT = 20.0

#: The grace a node that has just exited gets before its command gives up. Two
#: commands started at once can race for the node's port: the loser's node exits,
#: but the winner's answers as soon as it can, and that is the node to attach to.
_NODE_RACE_GRACE = 2.0

#: The one sentence a command states when no node can start on this machine. It
#: is the answer the direction records — the work needs a node, and there is no
#: in-process fallback — and it names the command that says *why* no node starts
#: rather than guessing at a cause. The ways it names to start one are the same
#: ones :data:`clear_record.core.node.NO_NODE_MESSAGE` names, and they are both
#: real for a bundle user whose CLI is not on ``PATH``: ``serve`` from a terminal,
#: the tray app (the double-click target) for the desktop.
_NODE_IS_THE_TOOL = deferred(
    "clear-record's work needs a node, and no node can start on this machine; "
    "start one with `clear-record serve`, or from the tray app — `serve` says why"
)

#: The sentence for a node that **did** start and is still alive, but that this
#: command cannot reach — the case a machine whose state directory cannot be
#: written produces: the node serves on and is only unfindable (the record
#: decision in :class:`clear_record.web.app.NodeServer`). That node is left
#: running, and the sentence states what this command knows — it started, its pid,
#: where its log went — rather than a cause it cannot know. In the case its own
#: comment names the node recorded **no** address, so the sentence may not presume
#: one: what this command knows is that *it* cannot reach the node.
_NODE_STARTED_BUT_UNFINDABLE = deferred(
    "a node started as pid {pid} but this command cannot reach it; it is left "
    "running, and its log is in the diagnostics sink"
)


def _no_node() -> NoReturn:
    """State the one no-node sentence and exit non-zero — the whole answer."""
    click.echo(tr(_NODE_IS_THE_TOOL), err=True)
    raise SystemExit(1)


def _node_argv() -> list[str]:
    """The argv that starts a node: this CLI's own ``serve``.

    From an installed ``clear-record`` that is ``python -m`` of this module, so
    the child is the interpreter running the command rather than whichever
    ``clear-record`` happens to be on ``PATH``. A **frozen** build (PyInstaller,
    ADR-0014) has neither an interpreter to name nor a module to run: its
    executable *is* the CLI — the CLI launcher (``cli_launch.py``, the file the
    spec builds ``clear-record`` from) feeds ``sys.argv[1:]`` straight to
    :func:`main`, where the ``web`` and ``tray`` launchers prepend their verb —
    so the child is ``<bundle> serve``.

    The bind stays unstated on purpose: ``serve`` reads the node's default from
    :mod:`clear_record.core.node`, the one declaration of it, so the address the
    node records is the one every surface resolves.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve"]
    return [sys.executable, "-m", "clear_record.cli.cli", "serve"]


def _start_node() -> subprocess.Popen:
    """Start a node as a process of its own and return it.

    Detached — its own session and no terminal — because a node a command starts
    has to outlive the command: the next command finds it through the recorded
    address instead of starting a second one. Its own logs are the diagnostics
    sink's (``serve``'s posture), so nothing of the node's startup leaks onto this
    command's streams.
    """
    return subprocess.Popen(
        _node_argv(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_node(*, timeout: float = _NODE_START_TIMEOUT) -> node.NodeAddress:
    """The address of a node that answers, starting one when none does.

    The **attach path first**: :func:`clear_record.core.node.ask` resolves the
    recorded address *and* completes one request against it, so a node already up
    is used exactly as every other surface uses it, and no second node is started.
    Only when nothing answers is a node started — and then the address returned is
    the one the node **recorded**, the socket it really bound, never the port it
    was asked for.

    A machine where no node can start is not a machine where the command does the
    work itself: :data:`_NODE_IS_THE_TOOL` is stated and the command exits
    non-zero — there is no in-process fallback. A node that started and is
    *alive* is a different case and gets its own sentence: it is left running,
    because a node that runs is a node, and this command cannot know why it cannot
    be reached.

    The address is the caller's to use, and ``run`` uses it: the run it starts is
    submitted to the node this ensured (:func:`_cmd_run`), so the work happens
    where the node is rather than in this process.
    """
    try:
        return node.ask()
    except node.NoNodeError:
        pass
    try:
        child = _start_node()
    except OSError:
        # No interpreter, or no room for one more process: a node that cannot
        # even be started is the no-node case, sentence and exit.
        _no_node()
    deadline = time.monotonic() + timeout
    stopped_at: float | None = None
    while True:
        try:
            return node.ask()
        except node.NoNodeError:
            pass
        now = time.monotonic()
        if child.poll() is not None:
            # The node process gave up; another command may still have won the
            # race for the port, so give it its grace.
            if stopped_at is None:
                stopped_at = now
            if now >= stopped_at + _NODE_RACE_GRACE:
                break
        if now >= deadline:
            break
        time.sleep(0.1)
    if child.poll() is None:
        # A node that did start is not the no-node case, and it is not debris: it
        # is alive and unfindable — the node that serves on where the state
        # directory cannot be written. It is left running.
        click.echo(tr(_NODE_STARTED_BUT_UNFINDABLE, pid=child.pid), err=True)
        raise SystemExit(1)
    _no_node()


def _cmd_node(**kwargs: Any) -> int:
    """Print where the node is and prove it answers — never a port scan."""
    try:
        address = node.ask()
    except node.NoNodeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(tr("the node is listening at {url}", url=address.url))
    return 0


def _cmd_synth(**kwargs: Any) -> int:
    synth(
        kwargs["directory"],
        devices=kwargs["devices"],
        duration_s=kwargs["duration"],
        speakers=kwargs["speakers"],
        seed=kwargs["seed"],
    )
    return 0


def _cmd_ingest(**kwargs: Any) -> int:
    report = stages.ingest(
        kwargs["directory"],
        audio_files=list(kwargs["inputs"]) or None,
        split=_split_value(kwargs["split_channels"], kwargs["mix_down"]),
        on_event=_StageLines(),
    )
    _render_ingest(report, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_align(**kwargs: Any) -> int:
    alignment = stages.align(kwargs["directory"], reference=kwargs["reference"])
    _render_align(alignment, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_transcribe(**kwargs: Any) -> int:
    options = _options(kwargs)
    report = stages.transcribe(
        kwargs["directory"],
        options.backend,
        model=options.model,
        language=options.language,
        model_dir=options.model_dir,
        glossary=options.glossary,
        chunk_seconds=options.chunk_seconds,
        overlap_seconds=options.overlap_seconds,
        resume=options.resume,
        jobs=options.jobs,
        check_plugin=options.check_plugin,
        rerun_sources=options.rerun_sources,
        rerun_range=options.rerun_range,
        on_event=_StageLines(),
        # The decoder knobs are the declaration's; read them off the resolved
        # options rather than relisting them (unset stays None = no flag).
        **{knob.name: getattr(options, knob.name) for knob in DECODER_KNOBS},
    )
    _render_transcribe(report, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_diarize(**kwargs: Any) -> int:
    # The command is `diarize`; it has no on/off flag to read (see
    # `_DIARIZE_ONLY`), so it simply runs the stage.
    stages.diarize(
        kwargs["directory"],
        speakers=kwargs["speakers"],
        on_event=_StageLines(),
    )
    return 0


def _cmd_attribute(**kwargs: Any) -> int:
    report = stages.attribute(
        kwargs["directory"],
        mixed_source=kwargs["mixed_source"],
        window_s=kwargs["window_s"],
    )
    _render_attribute(report, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_glossary(**kwargs: Any) -> int:
    report = stages.glossary(kwargs["directory"], add=list(kwargs["add"]) or None)
    _render_glossary(report, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_reconcile(**kwargs: Any) -> int:
    record = stages.reconcile(kwargs["directory"], prefer=kwargs["reference"])
    _render_reconcile(record, Workspace.at(kwargs["directory"]))
    return 0


def _cmd_export(**kwargs: Any) -> int:
    written = stages.export(kwargs["directory"])
    _render_export(written, Workspace.at(kwargs["directory"]))
    return 0


# --------------------------------------------------------------------------- #
# `run`: the one command whose stages execute on the node
# --------------------------------------------------------------------------- #
def _asked(name: str, value: Any) -> bool:
    """Whether option *name* was set rather than left at the parser's default.

    An unset knob or flag is ``None`` (the knob table's own sentinel), ``False``
    or empty. ``--models-dir`` is the one exception: its parser default is a
    *value* — the directory this machine resolves (``resolve_models_dir``, a
    click default callable) — so it counts as asked only when the parsed value
    differs from that. ``0`` counts as asked: a false-y value the user typed is
    still a value (``--window-s 0``).
    """
    if name == "models_dir":
        return value != resolve_models_dir()
    return value is not None and value is not False and value != () and value != ""


#: The run knobs a node run cannot carry yet, from the knob declaration itself:
#: the rows whose value the node's run request has no field for (``jobs`` is one
#: it does), by the parsed argument each lands in and the flag a refusal names.
_NODE_RUN_UNSETTABLE = tuple(
    (knob.name, knob.cli[0]) for knob in RUN_KNOBS if knob.name != "jobs"
) + (
    ("models_dir", "--models-dir"),
    ("glossary", "--glossary"),
    ("check_plugin", "--check-plugin"),
    ("diarize", "--diarize"),
    ("no_diarize", "--no-diarize"),
    ("speakers", "--speakers"),
    ("attribute_energy", "--attribute-energy"),
    ("mixed_source", "--mixed-source"),
    ("window_s", "--window-s"),
    ("reference", "--reference"),
    ("rerun_sources", "--rerun-source"),
    ("rerun_range", "--rerun-range"),
)

#: The parsed `run` arguments the node's run request **does** carry, so a check
#: that every flag of `run` is accounted for can tell the two sets apart.
_NODE_RUN_CARRIED = frozenset(
    {
        "backend",
        "model",
        "language",
        "resume",
        "jobs",
        "profile",
        "auto",
        # One value between them (`--split-channels` / `--mix-down` → `split`).
        "split_channels",
        "mix_down",
    }
)


def _node_run_refusal(args: Any) -> str | None:
    """The sentence refusing the flags a node run cannot carry, or ``None``.

    A run on the node executes with the node's own run request, so a knob the
    request does not declare can neither be passed nor honoured. Dropping it
    silently is what this refuses: the user asked for it, and a run that ignored
    the request while reporting success would be a lie about what ran — the same
    rule the chunk scope already states ("the scope is never quietly dropped").
    Every flag the user set is named in one sentence, so one edit fixes them all.
    """
    asked = [
        flag
        for name, flag in _NODE_RUN_UNSETTABLE
        if _asked(name, getattr(args, name, None))
    ]
    if not asked:
        return None
    return tr(
        "a node run cannot set {flags} yet: it runs with what the node's run API "
        "declares, and nothing is dropped",
        flags=", ".join(asked),
    )


def _node_run_body(args: Any, *, split: str) -> dict:
    """The node's own run request, filled from the parsed flags.

    Only the fields the node's run API declares are named here, with its names
    and its spellings for the shared ones; the flags it does not declare are
    refused by :func:`_node_run_refusal` before this is reached. The origin is the
    command line's own (``cli``), which is what puts this run in the console's
    list as one the command line started.

    ``split`` is the value the shared channel mapper produced
    (:func:`_split_value`), passed in by the caller that already ran it — the
    mapping is stated once, for both the validation and the request.
    """
    return {
        "backend": args.backend,
        "model": args.model,
        "language": args.language,
        "split": split,
        "resume": args.resume,
        "jobs": args.jobs or 0,
        "profile": args.profile or PROFILE_CUSTOM,
        "auto": bool(args.auto),
        "origin": CLI_ORIGIN,
    }


_T = TypeVar("_T")


def _node_step(step: Callable[[], _T]) -> _T:
    """Run one step of this client's path to the node, or end on its answer.

    Every step that can still lose the node — the submission, and each read that
    follows it; the address itself is :func:`ensure_node`'s and never raises
    ``NoNodeError`` — can find the node gone: it stopped, or the address it
    recorded went stale. The sentence for that is ``node.NoNodeError``'s, the one
    every surface reports, and a person reads it from here rather than as a
    traceback. A **refusal** the node answered with (an HTTP answer, not a lost
    transport: the node's own ``detail``) ends the command the same way, and it
    reads the same in the middle of a run as at its submission.
    """
    try:
        return step()
    except node.NoNodeError as exc:
        raise SystemExit(str(exc)) from exc
    except Refused as exc:
        # The node's own sentence, rendered here because this is the surface a
        # person reads it on.
        raise SystemExit(tr(str(exc))) from exc


def _cmd_run(**kwargs: Any) -> int:
    """Run the workspace on the node, and follow the run the node owns.

    ADR-0032: a run started here is a **node run** — it joins the one run per node
    queue, is recorded with the ``cli`` origin, and is followed from the node. So
    this **ensures** a node: a recorded one that answers is attached to, and one is
    started when none does. A machine where no node can start is one sentence,
    never a local fallback — the node *is* the tool (:func:`ensure_node`) — and if
    the node goes away while the run it accepted is still being followed, the same
    one sentence stands, at whatever step of the run it happened
    (:func:`_node_step`).

    What ``--auto`` chose is explained here, in the resolver's own words: the
    resolution ran **on the node**, so its account comes back on the run the node
    accepted and is printed before the stages it resolved (``cli.runs``).

    Everything the flags alone can decide is decided **before** the node is
    touched — a usage error neither needs a node nor brings one up — and every
    refusal raises rather than returning a code, because a code this command
    returns would be swallowed by Click's standalone mode (the group is what the
    tests drive; ``main`` is what turns a return into an exit code).
    """
    args = SimpleNamespace(**kwargs)
    split = _split_value(args.split_channels, args.mix_down)
    _do_diarize(args.diarize, args.no_diarize)
    refusal = _node_run_refusal(args)
    if refusal is not None:
        raise click.UsageError(refusal)
    directory = str(Path(kwargs["directory"]).resolve())
    # The flags are settled — a usage error above never reaches a node. Every step
    # from here on needs one, and can find it gone: `ensure_node` answers the
    # absent case itself (it starts a node or states the one sentence), while the
    # submission and each read that follows it go through `_node_step` so a node
    # lost mid-run reads as that same sentence rather than as a traceback.
    address = ensure_node()
    started = _node_step(
        lambda: start_run(address, directory, _node_run_body(args, split=split))
    )
    for line in started.explanations:
        print(line)
    state = _node_step(lambda: follow_run(address, started.run_id))
    # The run's own end, in the node's own words: the status value (machine-read
    # by design, printed verbatim like the console's badge) and, when the run
    # wrote one, its stored reason — a message ID the service composed, rendered
    # here because this is where a person reads it.
    end = f"[run] #{started.run_id} {state.status}"
    if state.succeeded:
        print(end)
        print(
            tr(
                "[next] the record is in {directory}/export; review it and accept "
                "the minutes in the console: `clear-record web`",
                directory=directory,
            )
        )
        return 0
    print(end, file=sys.stderr)
    if state.error:
        print(tr(state.error), file=sys.stderr)
    raise SystemExit(1)


def _cmd_calibrate(**kwargs: Any) -> int:
    _run_pipeline(kwargs["directory"], _options(kwargs))
    calibrate_report(kwargs["directory"], reference=kwargs["reference_transcript"])
    return 0


# --------------------------------------------------------------------------- #
# group assembly
# --------------------------------------------------------------------------- #
# One command body per declared stage. The stage order, names and help text come
# from the spec; only the flags (what a stage accepts) are local. `--reference`
# is carried only by the stages that act on a source reference — `align` (the
# alignment anchor) and `reconcile` (recorded on the record) — plus the run
# conveniences above; the other stage commands refuse it rather than accept a
# flag they would drop.
_STAGE_COMMANDS: dict[Step, tuple[Any, tuple]] = {
    Step.INGEST: (_cmd_ingest, (_PATHS, _CHANNEL)),
    Step.ALIGN: (_cmd_align, (_DIRECTORY, _REFERENCE)),
    Step.TRANSCRIBE: (_cmd_transcribe, (_DIRECTORY, _BACKEND)),
    Step.RECONCILE: (_cmd_reconcile, (_DIRECTORY, _REFERENCE)),
    Step.EXPORT: (_cmd_export, (_DIRECTORY,)),
}


def _run_help() -> str:
    """``run``'s help: the stage order it walks, and the node it walks it on.

    The order is the pipeline's own declaration
    (:meth:`~clear_record.core.pipeline.PipelineSpec.run_help`); the node clause
    is this surface's, because ADR-0032 makes a command-line run the **node's** —
    it joins the node's queue, and a flag the node's run API does not carry is
    refused rather than dropped, which a user should know before typing it. The
    command ensures that node (it attaches to a recorded one, or starts one), so
    the clause says *ensures* rather than *needs*.
    """
    return (
        pipeline_spec().run_help()
        + " — on the node, which runs it: the run joins its one-run-at-a-time "
        "queue and shows in the console's Activity list (this command ensures a "
        "node — attaching to the recorded one or starting one; `clear-record "
        "node` prints where it is). A flag the node's run API does not carry is "
        "refused, never dropped."
    )


#: The subcommands that are not one declared stage: `run`/`calibrate` and the
#: pipeline's conveniences that are not stage-derived (diarize / attribute /
#: glossary) with the argparse-era help text, the development and diagnostic
#: commands (synth / backends), and `node`, the verb that reaches the recorded
#: address, whose help is a `deferred(...)` message ID like the other `deferred`
#: entries in this tuple.
_CONVENIENCE_COMMANDS: tuple[tuple[str, Any, str, tuple], ...] = (
    (
        "run",
        _cmd_run,
        _run_help(),
        (_DIRECTORY, _BACKEND, _CHANNEL, _DIARIZE, _ATTRIBUTE, _REFERENCE),
    ),
    (
        "calibrate",
        _cmd_calibrate,
        deferred(
            "run the pipeline and report transcript quality against a reference if given"
        ),
        (
            _DIRECTORY,
            _BACKEND,
            _CHANNEL,
            _DIARIZE,
            _ATTRIBUTE,
            _REFERENCE,
            _REFERENCE_TRANSCRIPT,
        ),
    ),
    (
        "diarize",
        _cmd_diarize,
        deferred("assign speaker labels to already-transcribed segments"),
        (_DIRECTORY, _DIARIZE_ONLY),
    ),
    (
        "attribute",
        _cmd_attribute,
        deferred(
            "re-attribute speakers by relative source energy (close-mic cross-talk)"
        ),
        (_DIRECTORY, _ATTRIBUTE_ONLY),
    ),
    (
        "glossary",
        _cmd_glossary,
        deferred("show or append to the workspace glossary (ASR initial prompt)"),
        (_DIRECTORY, _ADD),
    ),
    (
        "synth",
        _cmd_synth,
        deferred(
            "generate a clean scene + degraded per-device recordings with exact ground truth"
        ),
        (_DIRECTORY, _SYNTH),
    ),
    (
        "backends",
        _cmd_backends,
        deferred("list which ASR backends are currently available"),
        (_BACKEND_ALL,),
    ),
    (
        "node",
        _cmd_node,
        deferred("print where the running node is listening, and whether it answers"),
        (),
    ),
)


def _make_command(
    name: str, callback: Any, help_text: str, option_sets: Sequence
) -> click.Command:
    """Build a fresh Click command so repeated group builds cannot mutate each other."""

    def fn(**kwargs: Any) -> Any:
        return callback(**kwargs)

    fn.__name__ = f"_{name}"
    for option_set in reversed(tuple(option_sets)):
        fn = option_set(fn)
    return click.command(name=name, help=tr(help_text))(fn)


def _builtin_commands() -> list[tuple[str, Any, str, tuple]]:
    """The built-in command table: every stage first, in spec order, then extras."""
    commands: list[tuple[str, Any, str, tuple]] = []
    for stage in pipeline_spec().stages:
        callback, option_sets = _STAGE_COMMANDS[stage.step]
        commands.append((stage.step.value, callback, stage.help, option_sets))
    commands.extend(_CONVENIENCE_COMMANDS)
    return commands


def _build_group() -> click.Group:
    """Build the ``clear-record`` Click group.

    A fresh group per call, like the old ``_build_parser``: entry-point providers
    are re-discovered each time, so tests can substitute the provider list, and a
    plain CLI run still never imports an optional surface at module import.
    """
    group = _Group(
        name=_PROG,
        help=tr(_DESCRIPTION),
        params=[
            click.Option(
                ("-v", "--verbose"), is_flag=True, default=False, help=tr(_VERBOSE_HELP)
            ),
            # ``--lang`` is eager so a chosen catalog is installed before the
            # command body runs. Kept on the group (like ``-v``) so it reads as a
            # process-wide choice; ``CR_LANG``/``LANG`` cover the common case.
            click.Option(
                ("--lang",),
                is_eager=True,
                expose_value=False,
                callback=_install_language,
                help=_lang_help(),
            ),
        ],
    )
    for name, callback, help_text, option_sets in _builtin_commands():
        group.add_command(_make_command(name, callback, help_text, option_sets))

    # Optional surfaces (e.g. the bundled `web` console) add their subcommands
    # through an entry point instead of being imported here (ADR-0013).
    _register_external_subcommands(group)
    return group


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code.

    ``standalone_mode=False`` keeps ``main`` a plain function that *returns* an
    int (the console script and programmatic callers both rely on that); usage
    errors are converted here into Click's stderr message and exit code, exactly
    as ``argparse`` used to. ``set_level`` is owned by the group so repeated
    programmatic ``main()`` calls stay honest.

    The UI language is installed **before** the group is built: Click snapshots
    help strings at construction time, so ``clear-record --lang de --help`` has
    to resolve the flag up front (the group's eager ``--lang`` callback then
    re-installs the same catalog for the command body).
    """
    install_language(resolve_locale(_requested_lang(argv)))
    group = _build_group()
    try:
        rv = group.main(args=argv, prog_name=_PROG, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except stages.PipelineError as exc:
        # The pipeline's failure channel is not an exit: a stage raises the
        # message, and the command surface is what decides that a failure at the
        # process boundary means one. Same message, same exit code as the stage's
        # own ``SystemExit`` produced — ``str`` goes to stderr, status 1.
        raise SystemExit(str(exc)) from exc
    return 0 if rv is None else int(rv)


if __name__ == "__main__":  # pragma: no cover - console-script path
    raise SystemExit(main())
