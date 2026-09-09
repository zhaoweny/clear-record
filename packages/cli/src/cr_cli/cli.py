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

    # pipeline stages
    add_pipeline_command(
        "ingest", "discover/declare recording sources", lambda p: _paths(p)
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
        _backend(directory_first=True),
    )

    # calibration convenience
    cal = sub.add_parser(
        "calibrate",
        help="run the pipeline and report transcript quality against a reference if given",
    )
    cal.add_argument("directory", help="workspace directory")
    _backend_args(cal)
    cal.add_argument(
        "--reference-transcript",
        help="a reference transcript text file to compare (WER/similarity)",
    )

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

    if command == "ingest":
        stages.ingest(args.directory, audio_files=args.inputs or None)
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
        )
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
        )
        return 0

    if command == "calibrate":
        stages.run(
            args.directory,
            backend=args.backend,
            model=args.model,
            language=args.language,
            model_dir=args.models_dir,
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
