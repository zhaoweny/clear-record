"""Tests for cr-cli: the command surface matches the pipeline spec."""

from __future__ import annotations

import argparse

import pytest

from cr_core import pipeline_spec
from cr_cli import stages
from cr_cli.cli import _build_parser


def test_parser_prog_is_clear_record() -> None:
    """The usage/help command name matches the owner's spelling (ADR-0009)."""
    assert _build_parser().prog == "clear-record"


def test_subcommands_match_pipeline() -> None:
    parser = _build_parser()
    expected = set(pipeline_spec().cli_commands()) | {
        "backends",
        "run",
        "calibrate",
        "synth",
        "diarize",
        "attribute",
        "glossary",
    }
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(subparsers_action.choices) == expected


def test_pipeline_spec_is_the_one_source_of_stage_truth() -> None:
    """The spec covers all three consumers: itself, the CLI subcommands, and the
    `run` dispatch table. Order is asserted for the CLI (choices preserve
    insertion order); `run`'s execution order is pinned in test_cli_pipeline."""
    spec = pipeline_spec()
    parser = _build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )

    # CLI: the pipeline subcommands appear in the spec's order.
    declared = set(spec.cli_commands())
    ordered = [name for name in subparsers_action.choices if name in declared]
    assert ordered == list(spec.cli_commands())

    # run: the dispatch table covers exactly the spec's stages.
    assert set(stages._STAGE_RUNNERS) == set(spec.steps)


def test_backend_choices_come_from_the_catalog() -> None:
    """The `--backend` choices and default follow the provider catalog, so a new
    backend is offered without a CLI edit (no literal tuple)."""
    from cr_providers import BACKENDS

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    backend_action = next(
        a for a in sub.choices["transcribe"]._actions if a.dest == "backend"
    )
    assert tuple(backend_action.choices) == tuple(BACKENDS)
    assert backend_action.default == next(iter(BACKENDS))


def test_run_and_calibrate_expose_reference() -> None:
    parser = _build_parser()
    assert parser.parse_args(["run", "dir", "--reference", "b"]).reference == "b"
    assert parser.parse_args(["calibrate", "dir", "--reference", "b"]).reference == "b"


def test_attribute_surface_wires_energy_and_mixed_reference() -> None:
    """`attribute` and `run --attribute-energy` expose the cross-talk seam; the
    default `run` leaves energy attribution off (diarize path unchanged)."""
    parser = _build_parser()
    standalone = parser.parse_args(["attribute", "dir", "--mixed-source", "room"])
    assert standalone.mixed_source == "room"
    assert standalone.window_s is None

    windowed = parser.parse_args(["attribute", "dir", "--window-s", "15"])
    assert windowed.window_s == 15.0

    default = parser.parse_args(["run", "dir"])
    assert default.attribute_energy is False
    assert default.mixed_source is None
    assert default.window_s is None

    opted = parser.parse_args(
        ["run", "dir", "--attribute-energy", "--mixed-source", "room"]
    )
    assert opted.attribute_energy is True
    assert opted.mixed_source == "room"


def test_check_plugin_is_opt_in_surface() -> None:
    """The plugin-load probe is off by default and opt-in on the backend paths."""
    parser = _build_parser()
    assert parser.parse_args(["transcribe", "dir"]).check_plugin is False
    assert parser.parse_args(["run", "dir"]).check_plugin is False
    assert parser.parse_args(["transcribe", "dir", "--check-plugin"]).check_plugin
    assert parser.parse_args(["run", "dir", "--check-plugin"]).check_plugin


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
