"""The explainable recommended default: `--auto` and `--backend auto`.

The resolvers are pure over injected probes, so these drive them across whole
machines — 8 vs 24 GB VRAM, 4 vs 16 cores, model present/absent, mono vs
4-channel, long vs short tape — and assert both the choice and the explanation.
The CLI wiring is asserted separately, with the real probes monkeypatched, so
the default (no-flag) path is proved to run neither probe.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
import soundfile as sf
import numpy as np

from clear_record.cli import auto
from clear_record.cli.auto import (
    BACKEND_PREFERENCE,
    AutoProbe,
    NoBackendAvailable,
    resolve_auto,
    resolve_backend,
)
from clear_record.cli.cli import _build_group, _pipeline_options
from clear_record.core import PipelineOptions


def _probe(**over: object) -> AutoProbe:
    base: dict = dict(
        available_backends=("apple",),
        vram_gb=8.0,
        cpu_count=16,
        models_on_disk=frozenset({"small", "medium"}),
        duration_s=1800.0,
        channels=1,
        language=None,
    )
    base.update(over)
    return AutoProbe(**base)


# --- `--backend auto`: capability-driven choice ----------------------------- #
def test_backend_preference_is_native_first_then_whisper_cli() -> None:
    """The order is data, with the (unbuilt) native ids at the top."""
    assert BACKEND_PREFERENCE[:2] == ("apple-speech", "windows-ai")
    assert BACKEND_PREFERENCE[2:] == ("apple", "nvidia", "amd")


def test_resolve_backend_picks_the_first_available() -> None:
    chose = resolve_backend(["amd", "apple", "apple-speech"])
    assert chose.backend == "apple-speech"  # native beats the fallback
    assert "apple-speech" in chose.explanation
    assert "apple-speech, windows-ai, apple, nvidia, amd" in chose.explanation

    # With no native backend available, the whisper-cli family wins in order.
    assert resolve_backend(["amd", "nvidia"]).backend == "nvidia"
    assert resolve_backend(["amd"]).backend == "amd"


def test_resolve_backend_none_available_is_an_actionable_error() -> None:
    with pytest.raises(NoBackendAvailable) as err:
        resolve_backend([])
    message = str(err.value)
    assert "whisper-cli" in message
    assert "brew install whisper-cpp" in message
    assert "docs/adr/0005" in message
    assert "--backend" in message


def test_resolve_backend_ignores_ids_not_in_the_preference_order() -> None:
    with pytest.raises(NoBackendAvailable):
        resolve_backend(["something-else"])


# --- `--auto`: the profile/model resolver ----------------------------------- #
def test_model_is_largest_that_fits_the_vram() -> None:
    small = resolve_auto(_probe(vram_gb=8.0, models_on_disk=frozenset({"small"})))
    big = resolve_auto(
        _probe(vram_gb=24.0, models_on_disk=frozenset({"small", "large-v3"}))
    )
    # A large model has no headroom on 8 GB; 24 GB affords it. `medium` is the
    # 8 GB preference, but only `small` is on disk there, so it is used instead.
    assert small.preferred_model == "medium"
    assert small.model == "small"
    assert big.model == "large-v3"
    assert big.preferred_model == "large-v3"
    assert "24 GB" in big.explanation
    assert "large-v3" in big.explanation


def test_unprobed_vram_uses_the_8gb_floor() -> None:
    """`detect_vram_gb()` returns None with no GPU; the existing floor applies."""
    resolved = resolve_auto(_probe(vram_gb=None, models_on_disk=frozenset()))
    assert resolved.model == resolved.preferred_model
    assert "unprobed (8 GB floor)" in resolved.explanation


def test_cpu_count_reaches_the_choice_and_the_explanation() -> None:
    """With plenty of cores a short tape is worth `accurate`; a small box is not."""
    modest = resolve_auto(_probe(cpu_count=4, vram_gb=8.0, duration_s=600.0))
    roomy = resolve_auto(_probe(cpu_count=16, vram_gb=8.0, duration_s=600.0))
    assert modest.profile == "balanced"
    assert roomy.profile == "accurate"
    assert "4 CPU(s)" in modest.explanation
    assert "16 CPU(s)" in roomy.explanation


def test_prefers_a_model_already_on_disk_over_downloading() -> None:
    # The VRAM-preferred `medium` is absent, but `small` is on disk: use it.
    resolved = resolve_auto(_probe(vram_gb=8.0, models_on_disk=frozenset({"small"})))
    assert resolved.model == "small"
    assert resolved.model_on_disk is True
    assert resolved.preferred_model == "medium"
    assert "'medium' is absent" in resolved.explanation
    assert "does not download" in resolved.explanation


def test_reports_an_absent_model_instead_of_fetching() -> None:
    resolved = resolve_auto(_probe(models_on_disk=frozenset()))
    assert resolved.model == resolved.preferred_model
    assert resolved.model_on_disk is False
    assert "none is on disk" in resolved.explanation


def test_multichannel_tapes_turn_on_per_speaker_attribution() -> None:
    mono = resolve_auto(_probe(channels=1))
    stereo = resolve_auto(_probe(channels=2))
    quad = resolve_auto(_probe(channels=4))
    assert mono.diarize is False
    assert stereo.diarize is False
    assert quad.diarize is True
    assert "per-speaker attribution on" in quad.explanation
    assert "per-speaker attribution" not in mono.explanation


def test_tape_length_and_headroom_pick_the_profile() -> None:
    roomy_short = resolve_auto(_probe(vram_gb=24.0, duration_s=600.0))
    roomy_long = resolve_auto(_probe(vram_gb=24.0, duration_s=3 * 3600.0))
    modest_long = resolve_auto(_probe(vram_gb=8.0, cpu_count=4, duration_s=3 * 3600.0))
    middle = resolve_auto(_probe(vram_gb=8.0, cpu_count=4, duration_s=1800.0))
    assert roomy_short.profile == "accurate"
    assert roomy_long.profile == "balanced"
    assert modest_long.profile == "fast"
    assert middle.profile == "balanced"
    assert "10 min" in roomy_short.explanation
    assert "3.0 h" in roomy_long.explanation

    # An empty/unknown tape is not "short" — fall back to the middle preset.
    unknown = resolve_auto(_probe(duration_s=0.0))
    assert unknown.profile == "balanced"
    assert "unknown" in unknown.explanation


def test_explanation_names_every_input_behind_the_choice() -> None:
    resolved = resolve_auto(
        _probe(
            available_backends=("apple", "amd"),
            vram_gb=24.0,
            cpu_count=16,
            models_on_disk=frozenset({"medium", "large-v3"}),
            duration_s=600.0,
            channels=4,
        )
    )
    for token in (
        "profile 'accurate'",
        "model 'large-v3'",
        "10 min",
        "4 channel(s)",
        "language auto",
        "24 GB",
        "16 CPU(s)",
        "large-v3, medium",
        "apple, amd",
    ):
        assert token in resolved.explanation, token


# --- real probe helpers ----------------------------------------------------- #
def test_models_on_disk_normalizes_ggml_names(tmp_path) -> None:
    (tmp_path / "ggml-small.bin").write_bytes(b"x")
    (tmp_path / "ggml-large-v3-q5_0.bin").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore me")
    assert auto.models_on_disk(str(tmp_path)) == frozenset({"small", "large-v3"})
    assert auto.models_on_disk(str(tmp_path / "missing")) == frozenset()


def test_tape_probe_reads_duration_and_channels(tmp_path) -> None:
    mono = np.zeros(8000, dtype="float32")
    quad = np.zeros((16000, 4), dtype="float32")
    sf.write(str(tmp_path / "mono.wav"), mono, 8000)
    sf.write(str(tmp_path / "quad.wav"), quad, 8000)
    assert auto.max_channels(tmp_path) == 4
    assert auto.tape_duration_s(tmp_path) == pytest.approx(2.0, abs=1e-3)


# --- CLI wiring ------------------------------------------------------------- #
def _args(argv: list[str]):
    command, *rest = argv
    cmd = _build_group().commands[command]
    with cmd.make_context(command, list(rest)) as ctx:
        return SimpleNamespace(**ctx.params)


def test_no_auto_flags_runs_no_probe_and_is_unchanged(monkeypatch) -> None:
    def _boom(*_a, **_k):  # pragma: no cover - a call here is the failure
        raise AssertionError("a probe ran on the default path")

    monkeypatch.setattr(auto, "probe_auto", _boom)
    monkeypatch.setattr("clear_record.cli.cli.available_backend_ids", _boom)

    opts = _pipeline_options(_args(["run", "dir"]))
    assert opts == PipelineOptions(model_dir=opts.model_dir)


def test_backend_auto_wires_the_capability_choice(monkeypatch, capsys) -> None:
    monkeypatch.setattr("clear_record.cli.cli.available_backend_ids", lambda: ("amd",))
    opts = _pipeline_options(_args(["run", "dir", "--backend", "auto"]))
    assert opts.backend == "amd"
    assert "--backend auto: chose 'amd'" in capsys.readouterr().out


def test_backend_auto_nothing_available_exits_actionably(monkeypatch) -> None:
    monkeypatch.setattr("clear_record.cli.cli.available_backend_ids", lambda: ())
    with pytest.raises(SystemExit) as err:
        _pipeline_options(_args(["run", "dir", "--backend", "auto"]))
    assert "whisper-cli" in str(err.value)


def test_auto_applies_profile_and_model_and_prints(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        auto,
        "probe_auto",
        lambda *a, **k: _probe(
            vram_gb=8.0,
            cpu_count=16,
            models_on_disk=frozenset({"small", "medium"}),
            duration_s=600.0,
        ),
    )
    opts = _pipeline_options(_args(["run", "dir", "--auto"]))
    assert opts.profile == "accurate"  # short tape on a roomy machine
    assert opts.model == "medium"
    assert "--auto: chose" in capsys.readouterr().out


def test_auto_explicit_flags_still_win(monkeypatch) -> None:
    monkeypatch.setattr(
        auto,
        "probe_auto",
        lambda *a, **k: _probe(
            models_on_disk=frozenset({"small", "medium"}), duration_s=600.0
        ),
    )
    opts = _pipeline_options(
        _args(["run", "dir", "--auto", "--model", "tiny", "--profile", "fast"])
    )
    assert opts.model == "tiny"
    assert opts.profile == "fast"


def test_auto_enables_diarization_only_when_the_user_left_it_unset(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auto,
        "probe_auto",
        lambda *a, **k: _probe(channels=4, models_on_disk=frozenset({"medium"})),
    )
    assert _pipeline_options(_args(["run", "dir", "--auto"])).do_diarize is True
    assert (
        _pipeline_options(_args(["run", "dir", "--auto", "--no-diarize"])).do_diarize
        is False
    )


def test_auto_refuses_an_absent_model_without_downloading(monkeypatch) -> None:
    """The no-download guarantee: report the absent model, never fetch it."""
    from clear_record.providers import backends as providers_backends

    def _no_download(*_a, **_k):  # pragma: no cover - a call here is the failure
        raise AssertionError("--auto triggered a model download")

    monkeypatch.setattr(providers_backends, "_download_ggml_model", _no_download)
    monkeypatch.setattr(
        auto, "probe_auto", lambda *a, **k: _probe(models_on_disk=frozenset())
    )
    with pytest.raises(SystemExit) as err:
        _pipeline_options(_args(["run", "dir", "--auto"]))
    message = str(err.value)
    assert "never downloads" in message
    assert "hf download" in message


def test_auto_is_a_noop_when_a_model_flag_is_given(monkeypatch) -> None:
    """`--model` is explicit, so `--auto` must not second-guess it."""
    monkeypatch.setattr(
        auto, "probe_auto", lambda *a, **k: _probe(models_on_disk=frozenset())
    )
    opts = _pipeline_options(_args(["run", "dir", "--auto", "--model", "base"]))
    assert opts.model == "base"
    assert opts.profile == "balanced"


# --- the profile resolver is pure ------------------------------------------- #
def test_resolve_auto_does_not_mutate_or_touch_io() -> None:
    probe = _probe()
    before = dataclasses.asdict(probe)
    resolve_auto(probe)
    assert dataclasses.asdict(probe) == before
