"""The CLI profile/decoder surface and its precedence.

The parser carries the flags; :func:`clear_record.cli.cli._pipeline_options`
resolves them against the environment and the profile. Keeping the last step
here (rather than only in ``core``) proves the wiring, not just the pure merge.
"""

from __future__ import annotations

import argparse
import os

import pytest

from clear_record.core import PROFILES, PipelineOptions
from clear_record.cli.cli import _build_parser, _pipeline_options

DECODER_FLAGS = {
    "beam_size": "--beam-size",
    "best_of": "--best-of",
    "temperature": "--temperature",
    "entropy_thold": "--entropy-thold",
    "no_speech_thold": "--no-speech-thold",
    "max_context": "--max-context",
    "threads": "--threads",
}


@pytest.fixture(autouse=True)
def _clear_cr_env(monkeypatch):
    """Resolution reads the real environment; keep it from leaking between tests."""
    for key in list(os.environ):
        if key.startswith("CR_"):
            monkeypatch.delenv(key, raising=False)


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return action.choices[name]


def _action(parser: argparse.ArgumentParser, dest: str) -> argparse.Action:
    return next(a for a in parser._actions if a.dest == dest)


def test_profile_and_decoder_flags_default_to_unset_on_every_backend_command() -> None:
    parser = _build_parser()
    for command in ("transcribe", "run", "calibrate"):
        ns = parser.parse_args([command, "dir"])
        assert ns.profile == "custom"
        # The resolver-managed knobs default to None (unset), not to the concrete
        # built-in value, so an explicit `--jobs 0` stays explicit.
        for field in ("chunk_seconds", "overlap_seconds", "jobs", *DECODER_FLAGS):
            assert getattr(ns, field) is None, f"{command} defaulted {field}"


def test_profile_choices_come_from_the_table() -> None:
    parser = _build_parser()
    action = _action(_subparser(parser, "run"), "profile")
    assert tuple(action.choices) == tuple(PROFILES)


def test_decoder_flags_parse_on_run() -> None:
    parser = _build_parser()
    ns = parser.parse_args(
        [
            "run",
            "dir",
            "--profile",
            "balanced",
            "--beam-size",
            "4",
            "--best-of",
            "3",
            "--temperature",
            "0.2",
            "--entropy-thold",
            "2.0",
            "--no-speech-thold",
            "0.5",
            "--max-context",
            "64",
            "--threads",
            "8",
        ]
    )
    assert ns.profile == "balanced"
    assert ns.beam_size == 4
    assert ns.best_of == 3
    assert ns.temperature == pytest.approx(0.2)
    assert ns.entropy_thold == pytest.approx(2.0)
    assert ns.no_speech_thold == pytest.approx(0.5)
    assert ns.max_context == 64
    assert ns.threads == 8


def test_pipeline_options_applies_the_profile() -> None:
    parser = _build_parser()
    opts = _pipeline_options(parser.parse_args(["run", "dir", "--profile", "accurate"]))
    assert isinstance(opts, PipelineOptions)
    assert opts.profile == "accurate"
    assert opts.beam_size == 8


def test_pipeline_options_flag_beats_profile() -> None:
    parser = _build_parser()
    opts = _pipeline_options(
        parser.parse_args(["run", "dir", "--profile", "accurate", "--beam-size", "2"])
    )
    assert opts.beam_size == 2


def test_pipeline_options_env_beats_profile(monkeypatch) -> None:
    monkeypatch.setenv("CR_BEAM_SIZE", "3")
    parser = _build_parser()
    opts = _pipeline_options(parser.parse_args(["run", "dir", "--profile", "accurate"]))
    assert opts.beam_size == 3


def test_pipeline_options_flag_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("CR_BEAM_SIZE", "3")
    parser = _build_parser()
    opts = _pipeline_options(parser.parse_args(["run", "dir", "--beam-size", "2"]))
    assert opts.beam_size == 2


def test_pipeline_options_explicit_jobs_zero_beats_profile_and_env(monkeypatch) -> None:
    """The regression Fix 1 guards: `--jobs 0` (auto) is explicit, so neither a
    profile nor `CR_JOBS` may replace the user's own flag."""
    monkeypatch.setenv("CR_JOBS", "5")
    parser = _build_parser()

    opts = _pipeline_options(
        parser.parse_args(["run", "dir", "--profile", "fast", "--jobs", "0"])
    )
    assert opts.jobs == 0

    # The same for an explicit chunk value equal to the built-in default.
    monkeypatch.setenv("CR_CHUNK_SECONDS", "300")
    opts = _pipeline_options(
        parser.parse_args(["run", "dir", "--chunk-seconds", "600"])
    )
    assert opts.chunk_seconds == 600.0


def test_pipeline_options_unset_jobs_still_means_auto(monkeypatch) -> None:
    monkeypatch.delenv("CR_JOBS", raising=False)
    parser = _build_parser()
    opts = _pipeline_options(parser.parse_args(["run", "dir"]))
    assert opts.jobs == 0
    assert opts.chunk_seconds == 600.0


def test_pipeline_options_without_profile_is_unchanged() -> None:
    parser = _build_parser()
    opts = _pipeline_options(parser.parse_args(["run", "dir"]))
    assert opts == PipelineOptions(
        # The CLI resolves the models dir, so it is an explicit field; everything
        # else must equal the built-in default.
        model_dir=opts.model_dir,
    )
