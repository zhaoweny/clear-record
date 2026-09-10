"""The `clearrecord` CLI.

One subcommand per pipeline stage (plus `run`/`calibrate` conveniences). The
subcommand surface is derived from the pipeline spec so the CLI and domain cannot
drift; the heavy per-stage logic lives in :mod:`cr_cli.stages`.

Recordings and model weights are environment-local data — never commit them.
See docs/architecture.md §6 and ADR-0006.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from cr_providers import available_backend_ids
from cr_engine import DEFAULT_CHUNK_S, DEFAULT_OVERLAP_S

from cr_cli import stages

_ENV_MODELS_DIR = "CR_MODELS_DIR"


def _default_models_dir() -> str:
    return os.environ.get(_ENV_MODELS_DIR, str(Path.cwd() / "models"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clearrecord",
        description="clear-record: from many recordings to one clear record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pipeline_command(name: str, help_: str, args_fn) -> None:
        p = sub.add_parser(name, help=help_)
        args_fn(p)
        _common_args(p)
        return p

    def _paths(p: argparse.ArgumentParser) -> None:
        p.add_argument("directory", help="workspace directory (recordings live here)")
        p.add_argument(
            "inputs",
            nargs="*",
            help="explicit audio file(s); default: scan the directory",
        )

    def _backend_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--backend",
            "-b",
            default="apple",
            choices=("apple", "nvidia", "amd"),
            help="ASR backend (default apple)",
        )
        p.add_argument(
            "--model",
            "-m",
            help="model checkpoint name/size, e.g. tiny/base/small/medium",
        )
        p.add_argument("--language", "-l", help="language hint for ASR (default auto)")
        p.add_argument(
            "--models-dir",
            default=_default_models_dir(),
            help=f"model download dir (default {_default_models_dir()})",
        )
        p.add_argument(
            "--glossary",
            help="glossary file (one term/line) used as the ASR initial prompt; "
            "defaults to <directory>/glossary.txt if present",
        )
        p.add_argument(
            "--chunk-seconds",
            type=float,
            default=DEFAULT_CHUNK_S,
            help=f"chunk length for long tape transcription (default {DEFAULT_CHUNK_S:.0f}s)",
        )
        p.add_argument(
            "--overlap-seconds",
            type=float,
            default=DEFAULT_OVERLAP_S,
            help=f"overlap between chunks (default {DEFAULT_OVERLAP_S:.0f}s)",
        )
        p.add_argument(
            "--no-resume",
            dest="resume",
            action="store_false",
            help="ignore cached chunks and re-transcribe from scratch",
        )
        p.set_defaults(resume=True)

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

    # pipeline stages
    add_pipeline_command(
        "ingest",
        "discover/declare recording sources",
        lambda p: (_paths(p), _channel_args(p)),
    )
    add_pipeline_command(
        "align", "estimate source time offsets onto a common clock", _directory
    )
    add_pipeline_command(
        "transcribe",
        "run a chosen ASR backend over each source",
        _backend(directory_first=True),
    )
    add_pipeline_command(
        "reconcile",
        "merge segments into an attributed, aligned timeline",
        _directory,
    )
    add_pipeline_command("export", "write Markdown/SRT/VTT/JSON artifacts", _directory)
    add_pipeline_command(
        "run",
        "full pipeline: ingest -> align -> transcribe -> reconcile -> export",
        lambda p: (
            _backend(directory_first=True)(p),
            _channel_args(p),
            _diarize_args(p),
        ),
    )

    # calibration convenience
    cal = sub.add_parser(
        "calibrate",
        help="run the pipeline and report transcript quality against a reference if given",
    )
    cal.add_argument("directory", help="workspace directory")
    _backend_args(cal)
    _channel_args(cal)
    _diarize_args(cal)
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

    return parser


def _main(args: argparse.Namespace) -> int:
    command = args.command

    if command == "backends":
        known = ("apple", "nvidia", "amd")
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
        stages.transcribe(
            args.directory,
            args.backend,
            model=args.model,
            language=args.language,
            model_dir=args.models_dir,
            glossary=args.glossary,
            chunk_seconds=args.chunk_seconds,
            overlap_seconds=args.overlap_seconds,
            resume=args.resume,
        )
        return 0

    if command == "diarize":
        stages.diarize(args.directory, speakers=args.speakers)
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
        stages.run(
            args.directory,
            backend=args.backend,
            model=args.model,
            language=args.language,
            model_dir=args.models_dir,
            audio_files=None,
            split=args.split,
            glossary=args.glossary,
            chunk_seconds=args.chunk_seconds,
            overlap_seconds=args.overlap_seconds,
            resume=args.resume,
            do_diarize=args.diarize,
            speakers=args.speakers,
            reference=args.reference,
        )
        return 0

    if command == "calibrate":
        stages.run(
            args.directory,
            backend=args.backend,
            model=args.model,
            language=args.language,
            model_dir=args.models_dir,
            split=args.split,
            glossary=args.glossary,
            chunk_seconds=args.chunk_seconds,
            overlap_seconds=args.overlap_seconds,
            resume=args.resume,
            do_diarize=args.diarize,
            speakers=args.speakers,
            reference=args.reference,
        )
        stages.calibrate_report(args.directory, reference=args.reference_transcript)
        return 0

    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _main(args)


if __name__ == "__main__":  # pragma: no cover - console-script path
    raise SystemExit(main())
