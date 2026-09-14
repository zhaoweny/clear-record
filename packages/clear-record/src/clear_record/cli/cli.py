"""The `clear-record` CLI.

One subcommand per pipeline stage (plus `run`/`calibrate` conveniences). The
subcommand surface is derived from the pipeline spec so the CLI and domain cannot
drift; the heavy per-stage logic lives in :mod:`clear_record.cli.stages`.

Recordings and model weights are environment-local data — never commit them.
See docs/architecture.md §6 and ADR-0006.
"""

from __future__ import annotations

import argparse
import dataclasses
from typing import Sequence

from clear_record.core import (
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    PROFILE_CUSTOM,
    PROFILES,
    PipelineOptions,
    Step,
    pipeline_spec,
    resolve_options,
)
from clear_record.providers import BACKENDS, available_backend_ids, resolve_models_dir

from clear_record.cli import auto
from clear_record.cli import stages

#: Entry-point group for optional subcommand providers. The bundled web console
#: registers here (ADR-0013) so this module never imports it, keeping the
#: dependency arrow acyclic and a plain CLI run cheap.
COMMAND_ENTRY_POINT_GROUP = "clear_record.commands"


def _external_commands():
    """Installed subcommand providers, ordered by entry-point name.

    Imported lazily so the base CLI carries no dependency on any provider.
    """
    from importlib.metadata import entry_points

    return sorted(entry_points(group=COMMAND_ENTRY_POINT_GROUP), key=lambda ep: ep.name)


def _register_external_subcommands(sub) -> None:
    """Let each installed provider add its subcommand to the parser."""
    for entry_point in _external_commands():
        entry_point.load()(sub)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clear-record",
        description="clear-record: from many recordings to one clear record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _paths(p: argparse.ArgumentParser) -> None:
        p.add_argument("directory", help="workspace directory (recordings live here)")
        p.add_argument(
            "inputs",
            nargs="*",
            help="explicit audio file(s); default: scan the directory",
        )

    def _decoder_args(p: argparse.ArgumentParser) -> None:
        """The whisper-cli decoder knobs. All default to unset (no flag added)."""
        p.add_argument(
            "--beam-size",
            type=int,
            default=None,
            help="beam search width; larger = slower and (usually) more accurate",
        )
        p.add_argument(
            "--best-of",
            type=int,
            default=None,
            help="candidates tried in greedy decoding; larger = slower",
        )
        p.add_argument(
            "--temperature",
            type=float,
            default=None,
            help="decoding temperature (0.0 = deterministic)",
        )
        p.add_argument(
            "--entropy-thold",
            type=float,
            default=None,
            help="entropy threshold; decoding stops when it falls below it",
        )
        p.add_argument(
            "--no-speech-thold",
            type=float,
            default=None,
            help="probability below which a window counts as silence/skip",
        )
        p.add_argument(
            "--max-context",
            type=int,
            default=None,
            help="max tokens of previous text used as decoder context (-1 = default)",
        )
        p.add_argument(
            "--threads",
            type=int,
            default=None,
            help="CPU threads for the decoder (matters on CPU-only paths)",
        )

    def _backend_args(p: argparse.ArgumentParser) -> None:
        default_backend = next(iter(BACKENDS))
        models_dir = resolve_models_dir()
        p.add_argument(
            "--backend",
            "-b",
            default=default_backend,
            choices=(*BACKENDS, auto.BACKEND_AUTO),
            help=f"ASR backend (default {default_backend}); "
            f"{auto.BACKEND_AUTO!r} picks the best available backend "
            "(native first, whisper-cli fallback)",
        )
        p.add_argument(
            "--auto",
            dest="auto",
            action="store_true",
            help="recommended default: inspect the machine and the tape, choose "
            "a profile and model, and explain the choice (never downloads a "
            "model; explicit flags still win)",
        )
        p.add_argument(
            "--model",
            "-m",
            help="model checkpoint name/size, e.g. tiny/base/small/medium",
        )
        p.add_argument("--language", "-l", help="language hint for ASR (default auto)")
        p.add_argument(
            "--models-dir",
            default=models_dir,
            help=f"model download dir (default {models_dir})",
        )
        p.add_argument(
            "--glossary",
            help="glossary file (one term/line) used as the ASR initial prompt; "
            "defaults to <directory>/glossary.txt if present",
        )
        # These default to None (unset), not to the concrete built-in value, so
        # resolve_options can tell an explicit `--chunk-seconds 600` from "not
        # given" and keep the explicit flag on top of any profile/env layer.
        p.add_argument(
            "--chunk-seconds",
            type=float,
            default=None,
            help=f"chunk length for long tape transcription (default {DEFAULT_CHUNK_S:.0f}s)",
        )
        p.add_argument(
            "--overlap-seconds",
            type=float,
            default=None,
            help=f"overlap between chunks (default {DEFAULT_OVERLAP_S:.0f}s)",
        )
        p.add_argument(
            "--no-resume",
            dest="resume",
            action="store_false",
            help="ignore cached chunks and re-transcribe from scratch",
        )
        p.set_defaults(resume=True)
        p.add_argument(
            "--jobs",
            "-j",
            type=int,
            default=None,
            help="parallel transcription workers (0 = auto; process-isolated "
            "backends only, e.g. the AMD/NVIDIA whisper-cli)",
        )
        p.add_argument(
            "--check-plugin",
            action="store_true",
            help="one-shot whisper-cli load probe to confirm the ggml GPU plugin "
            "actually loads before transcribing (opt-in; the default probe only "
            "checks that the plugin file is present)",
        )
        p.add_argument(
            "--profile",
            choices=tuple(PROFILES),
            default=None,
            help="a preset trading decoder effort at a fixed model; any explicit "
            "flag overrides it (default custom = set nothing). None (unset) "
            "lets --auto choose the profile",
        )
        _decoder_args(p)

    def _diarize_args(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument(
            "--diarize",
            dest="diarize",
            action="store_true",
            help="force multi-speaker diarization",
        )
        g.add_argument(
            "--no-diarize",
            dest="diarize",
            action="store_false",
            help="disable diarization",
        )
        p.set_defaults(diarize=None)
        p.add_argument(
            "--speakers",
            type=int,
            default=None,
            help="known number of speakers (default: estimate from the audio)",
        )

    def _attribute_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--attribute-energy",
            action="store_true",
            help="attribute speakers by relative source energy (close-mic "
            "cross-talk) instead of spectral diarization",
        )
        p.add_argument(
            "--mixed-source",
            default=None,
            help="manifest source id to use as the mixed/room reference for "
            "energy attribution",
        )
        p.add_argument(
            "--window-s",
            dest="window_s",
            type=float,
            default=None,
            help="seconds of causal history for a rolling per-source level "
            "(tracks drifting gain); omit for the static whole-recording level",
        )

    def _common_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--reference",
            dest="reference",
            help="reference source id for alignment (default: first)",
        )

    def _directory(p: argparse.ArgumentParser) -> None:
        p.add_argument("directory", help="workspace directory (recordings live here)")

    def _backend(directory_first: bool):
        def fn(p: argparse.ArgumentParser) -> None:
            if directory_first:
                _directory(p)
            _backend_args(p)

        return fn

    def _channel_args(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument(
            "--split-channels",
            dest="split",
            action="store_const",
            const="split",
            help="split every channel of a multichannel file into its own source",
        )
        g.add_argument(
            "--mix-down",
            dest="split",
            action="store_const",
            const="mix",
            help="always downmix multichannel audio to mono",
        )
        p.set_defaults(split="auto")

    # One CLI arg-builder per declared stage. The stage order, names and help
    # text come from the spec; only the flags (what a stage accepts) are local.
    stage_args = {
        Step.INGEST: lambda p: (_paths(p), _channel_args(p)),
        Step.ALIGN: _directory,
        Step.TRANSCRIBE: _backend(directory_first=True),
        Step.RECONCILE: _directory,
        Step.EXPORT: _directory,
    }
    for stage in pipeline_spec().stages:
        p = sub.add_parser(stage.step.value, help=stage.help)
        stage_args[stage.step](p)
        _common_args(p)

    # pipeline run: every stage above, in spec order
    run_parser = sub.add_parser("run", help=pipeline_spec().run_help())
    _backend(directory_first=True)(run_parser)
    _channel_args(run_parser)
    _diarize_args(run_parser)
    _attribute_args(run_parser)
    _common_args(run_parser)

    # calibration convenience
    cal = sub.add_parser(
        "calibrate",
        help="run the pipeline and report transcript quality against a reference if given",
    )
    cal.add_argument("directory", help="workspace directory")
    _backend_args(cal)
    _channel_args(cal)
    _diarize_args(cal)
    _attribute_args(cal)
    _common_args(cal)
    cal.add_argument(
        "--reference-transcript",
        help="a reference transcript text file to compare (WER/similarity)",
    )

    # diarization (multi-speaker attribution for a single mixed stream)
    dia = sub.add_parser(
        "diarize", help="assign speaker labels to already-transcribed segments"
    )
    dia.add_argument("directory", help="workspace directory")
    _diarize_args(dia)

    # cross-talk-aware attribution (close mics hear more than one speaker)
    attr = sub.add_parser(
        "attribute",
        help="re-attribute speakers by relative source energy (close-mic cross-talk)",
    )
    attr.add_argument("directory", help="workspace directory")
    attr.add_argument(
        "--mixed-source",
        default=None,
        help="manifest source id to use as the mixed/room reference",
    )
    attr.add_argument(
        "--window-s",
        dest="window_s",
        type=float,
        default=None,
        help="seconds of causal history for a rolling per-source level "
        "(tracks drifting gain); omit for the static whole-recording level",
    )

    # glossary (decoder initial prompt; edit while a pass runs in the background)
    glo = sub.add_parser(
        "glossary",
        help="show or append to the workspace glossary (ASR initial prompt)",
    )
    glo.add_argument("directory", help="workspace directory")
    glo.add_argument(
        "--add", nargs="*", default=None, help="term(s)/phrase(s) to append"
    )

    # synthesis (owner strategy: build the badness, keep the ground truth)
    syn = sub.add_parser(
        "synth",
        help="generate a clean scene + degraded per-device recordings with exact ground truth",
    )
    syn.add_argument("directory", help="output workspace directory")
    syn.add_argument(
        "--devices", type=int, default=4, help="number of recording devices (default 4)"
    )
    syn.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="scene duration in seconds (default 20)",
    )
    syn.add_argument(
        "--speakers",
        type=int,
        default=4,
        help="number of speakers in the scene (default 4)",
    )
    syn.add_argument("--seed", type=int, default=0, help="random seed")

    # backends
    b = sub.add_parser(
        "backends", help="list which ASR backends are currently available"
    )
    b.add_argument(
        "--all",
        action="store_true",
        help="list all known backends, not only available ones",
    )

    # Optional surfaces (e.g. the bundled `web` console) add their subcommands
    # through an entry point instead of being imported here (ADR-0013).
    _register_external_subcommands(sub)

    return parser


def _backend_option_kwargs(args: argparse.Namespace) -> dict:
    """The backend/decoder subset shared by `transcribe`, `run` and `calibrate`."""
    return {
        "backend": args.backend,
        "model": args.model,
        "language": args.language,
        "model_dir": args.models_dir,
        "glossary": args.glossary,
        "chunk_seconds": args.chunk_seconds,
        "overlap_seconds": args.overlap_seconds,
        "resume": args.resume,
        "jobs": args.jobs,
        "check_plugin": args.check_plugin,
        # The parser leaves `--profile` as `None` when unset (a real sentinel,
        # like the other resolver-managed knobs), so `--auto` can tell "choose
        # for me" from an explicit `--profile custom`.
        "profile": args.profile or PROFILE_CUSTOM,
        "beam_size": args.beam_size,
        "best_of": args.best_of,
        "temperature": args.temperature,
        "entropy_thold": args.entropy_thold,
        "no_speech_thold": args.no_speech_thold,
        "max_context": args.max_context,
        "threads": args.threads,
    }


def _apply_auto(args: argparse.Namespace, options: PipelineOptions) -> PipelineOptions:
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
            raise SystemExit(str(exc)) from exc
        print(backend_choice.explanation)
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
    print(choice.explanation)

    if options.model is None:
        if not choice.model_on_disk:
            raise SystemExit(
                f"[auto] the recommended model {choice.model!r} is not in the "
                "models directory, and --auto never downloads one.\n"
                f"  Pre-fetch it, e.g. `hf download ggerganov/whisper.cpp "
                f"ggml-{choice.model}.bin --local-dir {args.models_dir}`, or pass "
                "an explicit `--model`."
            )
        options = dataclasses.replace(options, model=choice.model)

    if choice.diarize and options.do_diarize is None:
        options = dataclasses.replace(options, do_diarize=True)

    # `--auto` supplies the profile only when the user did not name one; the
    # environment still beats it, and every explicit knob beats both.
    profile = args.profile if args.profile is not None else choice.profile
    return resolve_options(options, profile=profile)


def _pipeline_options(args: argparse.Namespace) -> PipelineOptions:
    """Fill the one run-options value from the parsed CLI arguments and resolve it.

    The environment and the profile fill any knob the user left at its default;
    an explicit flag wins (see :func:`clear_record.core.resolve_options`), and
    the opt-in ``--auto`` / ``--backend auto`` resolvers fill in last.
    """
    kwargs = _backend_option_kwargs(args)
    kwargs.update(
        split=getattr(args, "split", "auto"),
        do_diarize=getattr(args, "diarize", None),
        speakers=getattr(args, "speakers", None),
        reference=getattr(args, "reference", None),
        attribute_energy=getattr(args, "attribute_energy", False),
        mixed_source=getattr(args, "mixed_source", None),
        window_s=getattr(args, "window_s", None),
    )
    return _apply_auto(args, PipelineOptions(**kwargs))


def _main(args: argparse.Namespace) -> int:
    command = args.command

    if command == "backends":
        known = tuple(BACKENDS)
        available = available_backend_ids()
        for bid in known:
            state = "available" if bid in available else "unavailable"
            if not args.all and state != "available":
                continue
            print(f"{bid:8s} {state}")
        return 0

    if command == "synth":
        stages.synth(
            args.directory,
            devices=args.devices,
            duration_s=args.duration,
            speakers=args.speakers,
            seed=args.seed,
        )
        return 0

    if command == "ingest":
        stages.ingest(args.directory, audio_files=args.inputs or None, split=args.split)
        return 0

    if command == "align":
        stages.align(args.directory, reference=args.reference)
        return 0

    if command == "transcribe":
        options = _pipeline_options(args)
        stages.transcribe(
            args.directory,
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
            beam_size=options.beam_size,
            best_of=options.best_of,
            temperature=options.temperature,
            entropy_thold=options.entropy_thold,
            no_speech_thold=options.no_speech_thold,
            max_context=options.max_context,
            threads=options.threads,
        )
        return 0

    if command == "diarize":
        stages.diarize(args.directory, speakers=args.speakers)
        return 0

    if command == "attribute":
        stages.attribute(
            args.directory,
            mixed_source=args.mixed_source,
            window_s=args.window_s,
        )
        return 0

    if command == "glossary":
        stages.glossary(args.directory, add=args.add)
        return 0

    if command == "reconcile":
        stages.reconcile(args.directory, prefer=args.reference)
        return 0

    if command == "export":
        stages.export(args.directory)
        return 0

    if command == "run":
        stages.run(args.directory, _pipeline_options(args))
        return 0

    if command == "calibrate":
        stages.run(args.directory, _pipeline_options(args))
        stages.calibrate_report(args.directory, reference=args.reference_transcript)
        return 0

    # An externally registered subcommand (e.g. `web`) carries its own handler.
    handler = getattr(args, "handler", None)
    if handler is not None:
        return handler(args)

    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _main(args)


if __name__ == "__main__":  # pragma: no cover - console-script path
    raise SystemExit(main())
