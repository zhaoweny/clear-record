"""Tests for clear_record.cli: the command surface matches the pipeline spec."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from clear_record.core import pipeline_spec
from clear_record.cli import cli, stages
from clear_record.cli.cli import _build_group, _pipeline_options


def _parse(command: str, argv: list[str]) -> SimpleNamespace:
    """Parse one subcommand's arguments (Click `make_context`), without invoking."""
    cmd = _build_group().commands[command]
    with cmd.make_context(command, list(argv)) as ctx:
        return SimpleNamespace(**ctx.params)


def test_group_prog_is_clear_record() -> None:
    """The usage/help command name matches the owner's spelling (ADR-0009)."""
    assert _build_group().name == "clear-record"


def test_subcommands_match_pipeline(monkeypatch) -> None:
    """Built-ins are exactly the pipeline stages plus conveniences.

    Anything beyond that must be an installed entry-point provider (e.g. the
    bundled `web` console), so the built-in surface cannot drift while optional
    surfaces stay free to register themselves (ADR-0013). A provider may
    contribute more than one command — the `web` provider adds the interactive
    `web` and the headless `serve` — so the external set is what the providers
    actually add, not their entry-point *names*.
    """
    group = _build_group()
    builtin = set(pipeline_spec().cli_commands()) | {
        "backends",
        "run",
        "calibrate",
        "synth",
        "diarize",
        "attribute",
        "glossary",
    }
    choices = set(group.commands)
    assert builtin <= choices

    monkeypatch.setattr(cli, "_external_commands", lambda: [])
    without_providers = set(_build_group().commands)
    contributed = choices - without_providers
    assert choices - builtin == contributed


def test_pipeline_spec_is_the_one_source_of_stage_truth() -> None:
    """The spec covers all three consumers: itself, the CLI subcommands, and the
    `run` dispatch table. Order is asserted for the CLI (Click's command mapping
    preserves insertion order); `run`'s execution order is pinned in
    test_cli_pipeline."""
    spec = pipeline_spec()
    group = _build_group()

    # CLI: the pipeline subcommands appear in the spec's order.
    declared = set(spec.cli_commands())
    ordered = [name for name in group.commands if name in declared]
    assert ordered == list(spec.cli_commands())

    # run: the dispatch table covers exactly the spec's stages.
    assert set(stages._STAGE_RUNNERS) == set(spec.steps)


def test_backend_choices_come_from_the_catalog() -> None:
    """The `--backend` choices and default follow the provider catalog, so a new
    backend is offered without a CLI edit (no literal tuple)."""
    from clear_record.cli.auto import BACKEND_AUTO
    from clear_record.providers import BACKENDS

    command = _build_group().commands["transcribe"]
    backend = next(param for param in command.params if param.name == "backend")
    # The catalog plus the `auto` sentinel (capability-driven selection).
    assert tuple(backend.type.choices) == (*BACKENDS, BACKEND_AUTO)
    assert backend.default == next(iter(BACKENDS))


def test_run_and_calibrate_expose_reference() -> None:
    assert _parse("run", ["dir", "--reference", "b"]).reference == "b"
    assert _parse("calibrate", ["dir", "--reference", "b"]).reference == "b"


def test_attribute_surface_wires_energy_and_mixed_reference() -> None:
    """`attribute` and `run --attribute-energy` expose the cross-talk seam; the
    default `run` leaves energy attribution off (diarize path unchanged)."""
    standalone = _parse("attribute", ["dir", "--mixed-source", "room"])
    assert standalone.mixed_source == "room"
    assert standalone.window_s is None

    windowed = _parse("attribute", ["dir", "--window-s", "15"])
    assert windowed.window_s == 15.0

    default = _parse("run", ["dir"])
    assert default.attribute_energy is False
    assert default.mixed_source is None
    assert default.window_s is None

    opted = _parse("run", ["dir", "--attribute-energy", "--mixed-source", "room"])
    assert opted.attribute_energy is True
    assert opted.mixed_source == "room"


def test_check_plugin_is_opt_in_surface() -> None:
    """The plugin-load probe is off by default and opt-in on the backend paths."""
    assert _parse("transcribe", ["dir"]).check_plugin is False
    assert _parse("run", ["dir"]).check_plugin is False
    assert _parse("transcribe", ["dir", "--check-plugin"]).check_plugin
    assert _parse("run", ["dir", "--check-plugin"]).check_plugin


def test_contradictory_channel_flags_are_a_usage_error() -> None:
    """`--split-channels --mix-down` must fail loudly, not silently take the last."""
    result = CliRunner().invoke(
        _build_group(), ["run", "dir", "--split-channels", "--mix-down"]
    )
    assert result.exit_code == 2
    message = result.output + result.stderr
    assert "--split-channels" in message
    assert "--mix-down" in message


def test_channel_flags_still_work_one_at_a_time() -> None:
    assert (
        _pipeline_options(_parse("run", ["dir", "--split-channels"])).split == "split"
    )
    assert _pipeline_options(_parse("run", ["dir", "--mix-down"])).split == "mix"
    assert _pipeline_options(_parse("run", ["dir"])).split == "auto"


def test_contradictory_diarize_flags_are_a_usage_error() -> None:
    """`--diarize --no-diarize` must fail loudly, not silently take the last."""
    result = CliRunner().invoke(
        _build_group(), ["run", "dir", "--diarize", "--no-diarize"]
    )
    assert result.exit_code == 2
    message = result.output + result.stderr
    assert "--diarize" in message
    assert "--no-diarize" in message


def test_diarize_flags_still_work_one_at_a_time() -> None:
    assert _pipeline_options(_parse("run", ["dir", "--diarize"])).do_diarize is True
    assert _pipeline_options(_parse("run", ["dir", "--no-diarize"])).do_diarize is False
    assert _pipeline_options(_parse("run", ["dir"])).do_diarize is None


def test_eval_error_rates_perfect_and_bad() -> None:
    from clear_record.cli.eval import error_rates

    assert error_rates("hello world", "hello world")["wer"] == 0.0
    assert error_rates("hello world", "hello there")["wer"] > 0.0


@pytest.mark.parametrize(
    "text", ["你好世界", "The quick brown fox", "Mixed 中文 and english"]
)
def test_eval_tokenizes_nonempty(text: str) -> None:
    from clear_record.cli.eval import _tokenize

    assert len(_tokenize(text)) > 0
