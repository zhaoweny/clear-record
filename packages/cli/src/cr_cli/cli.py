"""The `clearrecord` CLI.

This is a *scaffold*: the subcommand surface is derived from the pipeline spec
so the CLI/domain cannot drift, but each pipeline step currently reports itself
as a placeholder rather than executing real work. Execution lands incrementally
per docs/architecture.md §8.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from cr_core import pipeline_spec
from cr_providers import available_backend_ids

_DESCRIPTION = "clear-record: from many recordings to one clear record."


def _build_parser() -> argparse.ArgumentParser:
    spec = pipeline_spec()
    parser = argparse.ArgumentParser(
        prog="clearrecord",
        description=_DESCRIPTION,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd in spec.cli_commands():
        p = sub.add_parser(cmd, help=f"{cmd}: a pipeline stage")
        p.add_argument("inputs", nargs="*", help="input/audio path(s)")

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
    spec = pipeline_spec()
    steps = set(spec.cli_commands())

    if args.command == "backends":
        known = ("apple", "nvidia", "amd")
        available = available_backend_ids()
        if args.all:
            rows = [
                (bid, "available" if bid in available else "unavailable")
                for bid in known
            ]
        else:
            rows = [(bid, "available") for bid in available]
        for bid, state in rows:
            print(f"{bid:8s} {state}")
        return 0

    if args.command in steps:
        print(
            f"[clearrecord] {args.command}: scaffold only — "
            f"pipeline execution for {args.command!r} is not implemented yet."
        )
        return 0

    # Unreachable with argparse required subparsers; defensive.
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _main(args)


if __name__ == "__main__":  # pragma: no cover - console-script path
    raise SystemExit(main())
