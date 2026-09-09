"""Smoke tests for cr-cli: the command surface matches the pipeline spec."""

from __future__ import annotations

import argparse

from cr_core import pipeline_spec
from cr_cli.cli import _build_parser


def test_subcommands_match_pipeline() -> None:
    parser = _build_parser()
    # Add `backends` (a meta command, not a pipeline step).
    expected = set(pipeline_spec().cli_commands()) | {"backends"}
    # argparse stores subparser actions; introspect them.
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(subparsers_action.choices) == expected
