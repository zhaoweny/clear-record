"""The shared run options and the profile table.

These pin the two promises of ticket 01: a profile is data whose keys are real
knobs (and ``custom`` sets none), and the resolution precedence is
``explicit > CR_* env > profile > built-in default`` — with no profile the value
is unchanged.
"""

from __future__ import annotations

import dataclasses

import pytest

from clear_record import core
from clear_record.core import (
    DECODER_KNOB_FIELDS,
    PROFILES,
    RESOLVABLE_FIELDS,
    RUN_KNOBS,
    DecoderKnobs,
    PipelineOptions,
    profile_values,
    resolve_options,
)
from clear_record.core import options as options_module


def _fields() -> set[str]:
    return {field.name for field in dataclasses.fields(PipelineOptions)}


def test_every_profile_key_is_a_real_knob() -> None:
    for name, values in PROFILES.items():
        unknown = set(values) - _fields()
        assert not unknown, f"profile {name!r} sets unknown knobs: {sorted(unknown)}"


def test_custom_is_empty_by_definition() -> None:
    assert PROFILES["custom"] == {}
    assert profile_values("custom") == {}


def test_profiles_never_choose_a_backend_or_a_model() -> None:
    """A profile tunes knobs only; backend/model stay explicit choices (owner
    decision, round-1 grilling)."""
    for name, values in PROFILES.items():
        assert "backend" not in values, f"profile {name!r} implies a backend"
        assert "model" not in values, f"profile {name!r} implies a model"


def test_unknown_profile_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        profile_values("turbo")
    with pytest.raises(ValueError, match="unknown profile"):
        resolve_options(PipelineOptions(), profile="turbo", environ={})


def test_profile_beats_the_builtin_default() -> None:
    resolved = resolve_options(PipelineOptions(profile="accurate"), environ={})
    assert resolved.beam_size == PROFILES["accurate"]["beam_size"]
    assert resolved.profile == "accurate"

    fast = resolve_options(PipelineOptions(profile="fast"), environ={})
    assert fast.best_of == PROFILES["fast"]["best_of"]


def test_explicit_flag_beats_the_profile() -> None:
    resolved = resolve_options(
        PipelineOptions(profile="accurate", beam_size=2), environ={}
    )
    assert resolved.beam_size == 2

    fast = resolve_options(PipelineOptions(profile="fast", best_of=9), environ={})
    assert fast.best_of == 9


def test_env_beats_the_profile() -> None:
    resolved = resolve_options(
        PipelineOptions(profile="accurate"),
        environ={"CR_BEAM_SIZE": "3"},
    )
    assert resolved.beam_size == 3

    fast = resolve_options(PipelineOptions(profile="fast"), environ={"CR_BEST_OF": "7"})
    assert fast.best_of == 7


def test_explicit_beats_the_env() -> None:
    resolved = resolve_options(
        PipelineOptions(beam_size=2),
        environ={"CR_BEAM_SIZE": "3"},
    )
    assert resolved.beam_size == 2


def test_explicit_default_value_beats_profile_and_env(monkeypatch) -> None:
    """A real sentinel: an explicit value that *equals* the built-in default
    (``--jobs 0``, the documented "auto") is explicit, so nothing may fill it."""
    # Env: `CR_JOBS=5` and `CR_CHUNK_SECONDS=300` would otherwise win.
    resolved = resolve_options(
        PipelineOptions(jobs=0, chunk_seconds=600.0),
        environ={"CR_JOBS": "5", "CR_CHUNK_SECONDS": "300"},
    )
    assert resolved.jobs == 0
    assert resolved.chunk_seconds == 600.0

    # Profile: a profile that sets a knob whose built-in default is a real value
    # must still lose to the explicit default.
    monkeypatch.setitem(core.PROFILES, "jobful", {"jobs": 4})
    resolved = resolve_options(PipelineOptions(profile="jobful", jobs=0), environ={})
    assert resolved.jobs == 0
    assert resolved.profile == "jobful"


def test_unset_resolvable_fields_fall_to_the_builtin_default() -> None:
    """The CLI passes ``None`` for an unset flag; the resolver applies the
    built-in default, so a no-flag run is identical to before."""
    unset = PipelineOptions(chunk_seconds=None, overlap_seconds=None, jobs=None)
    assert resolve_options(unset, environ={}) == PipelineOptions()


def test_no_profile_and_no_env_leaves_the_options_unchanged() -> None:
    """The byte-identical promise: a run that asks for nothing resolves to the
    built-in default value it would have had before profiles existed."""
    assert resolve_options(PipelineOptions(), environ={}) == PipelineOptions()


def test_env_applies_even_with_custom_profile() -> None:
    # `jobs=None` is "unset" (as the CLI passes it); a concrete `jobs=0` would be
    # an explicit choice and would win over the environment.
    resolved = resolve_options(
        PipelineOptions(jobs=None), environ={"CR_JOBS": "5", "CR_THREADS": "8"}
    )
    assert resolved.jobs == 5
    assert resolved.threads == 8
    assert resolved.profile == "custom"


def test_blank_or_unparseable_env_is_ignored() -> None:
    resolved = resolve_options(
        PipelineOptions(), environ={"CR_BEAM_SIZE": "  ", "CR_THREADS": "many"}
    )
    assert resolved.beam_size is None
    assert resolved.threads is None


def test_decoder_knobs_only_lists_the_set_ones() -> None:
    assert PipelineOptions().decoder_knobs() == {}
    assert PipelineOptions(beam_size=4, threads=8).decoder_knobs() == {
        "beam_size": 4,
        "threads": 8,
    }
    # An explicit zero (a real value) is still passed through, not dropped.
    assert PipelineOptions(temperature=0.0).decoder_knobs() == {"temperature": 0.0}


def test_every_declared_knob_is_a_pipeline_options_field() -> None:
    assert set(RESOLVABLE_FIELDS) <= _fields()


def test_the_decoder_annotation_block_is_the_decoder_rows() -> None:
    """The field annotations are the one thing Python makes a declarer write
    twice (``dataclasses`` reads them, so they cannot be generated from the
    table); this pins them to the decoder rows — a half-added knob fails here."""
    assert [field.name for field in dataclasses.fields(DecoderKnobs)] == list(
        DECODER_KNOB_FIELDS
    )


def test_declared_defaults_are_the_options_defaults() -> None:
    """A row's ``default`` is the resolver's fallback for that field; a drift
    between the two would quietly move a no-flag run off the built-in value."""
    for knob in RUN_KNOBS:
        assert getattr(PipelineOptions(), knob.name) == knob.default, knob.name


def test_every_declared_env_var_is_honoured() -> None:
    """The ``CR_*`` name a row declares is the one the resolver reads, and the
    row's converter is the one it parses with."""
    # What the CLI hands the resolver for a run that asked for nothing.
    asked_for_nothing = PipelineOptions(**dict.fromkeys(RESOLVABLE_FIELDS))
    for knob in RUN_KNOBS:
        raw = "4" if knob.convert is int else "0.5"
        resolved = resolve_options(asked_for_nothing, environ={knob.env: raw})
        assert getattr(resolved, knob.name) == knob.convert(raw), knob.name


def test_resolvable_fields_cover_every_profile_and_env_knob() -> None:
    """A profile/env key outside RESOLVABLE_FIELDS would be silently ignored, so
    every one of them must be resolvable."""
    profile_keys = {key for values in PROFILES.values() for key in values}
    env_keys = {name for name, _ in options_module._ENV_KNOBS.values()}
    assert profile_keys <= set(RESOLVABLE_FIELDS)
    assert env_keys <= set(RESOLVABLE_FIELDS)


def test_cli_service_and_core_share_the_same_options_type() -> None:
    """The ADR-0017 seam: callers reach the run options without importing ``cli``,
    and the CLI/Service names are the same object, not copies."""
    from clear_record.pipeline import stages
    from clear_record.service import PipelineOptions as ServiceOptions

    assert ServiceOptions is PipelineOptions
    assert stages.PipelineOptions is PipelineOptions
