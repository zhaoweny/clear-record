"""Tests for cr-cli: the command surface matches the pipeline spec."""

from __future__ import annotations

import argparse

import pytest

from cr_core import pipeline_spec
from cr_cli.cli import _build_parser


def test_subcommands_match_pipeline() -> None:
    parser = _build_parser()
    expected = set(pipeline_spec().cli_commands()) | {
        "backends",
        "run",
        "calibrate",
        "synth",
        "diarize",
        "glossary",
    }
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(subparsers_action.choices) == expected


def test_eval_error_rates_perfect_and_bad() -> None:
    from cr_cli.eval import error_rates

    assert error_rates("hello world", "hello world")["wer"] == 0.0
    assert error_rates("hello world", "hello there")["wer"] > 0.0


@pytest.mark.parametrize(
    "text", ["你好世界", "The quick brown fox", "Mixed 中文 and english"]
)
def test_eval_tokenizes_nonempty(text: str) -> None:
    from cr_cli.eval import _tokenize

    assert len(_tokenize(text)) > 0
