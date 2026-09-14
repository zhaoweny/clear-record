"""The console's reuse of the CLI's explainable ``--auto`` resolvers.

The pure resolvers have their own tests (``tests/cli/test_cli_auto.py``); these
cover the service seam the console calls: the layering-safe re-export, the
precedence mirror, the no-download refusal, the recorded provenance, and that
the default path runs no probe.
"""

from __future__ import annotations

import pytest

from clear_record.cli import auto as cli_auto
from clear_record.cli.auto import AutoProbe
from clear_record.core import PipelineOptions
from clear_record.service import auto as service_auto


def _probe(**over: object) -> AutoProbe:
    base: dict = dict(
        available_backends=("apple",),
        vram_gb=8.0,
        cpu_count=16,
        models_on_disk=frozenset({"small", "medium"}),
        duration_s=600.0,
        channels=1,
        language=None,
    )
    base.update(over)
    return AutoProbe(**base)


def test_the_resolvers_are_the_clis_own() -> None:
    """The seam re-exports, never reimplements: same objects, same wording."""
    assert service_auto.resolve_auto is cli_auto.resolve_auto
    assert service_auto.resolve_backend is cli_auto.resolve_backend
    assert service_auto.probe_auto is cli_auto.probe_auto
    assert service_auto.BACKEND_AUTO == cli_auto.BACKEND_AUTO


def test_the_default_path_runs_no_probe_and_equals_resolve_options(
    monkeypatch,
) -> None:
    def boom(*_a, **_k):  # pragma: no cover - a call here is the failure
        raise AssertionError("a probe ran without --auto / --backend auto")

    monkeypatch.setattr(service_auto, "probe_auto", boom)
    monkeypatch.setattr(service_auto, "available_backend_ids", boom)

    resolved = service_auto.resolve_run(PipelineOptions(), profile=None)
    assert resolved.options == PipelineOptions()
    assert resolved.meta == {}
    assert resolved.explanations == ()


def test_auto_fills_profile_model_and_records_the_explanation(monkeypatch) -> None:
    monkeypatch.setattr(service_auto, "probe_auto", lambda *a, **k: _probe())
    resolved = service_auto.resolve_run(
        PipelineOptions(), profile=None, auto=True, directory="/tape"
    )

    assert resolved.options.profile == "accurate"  # short tape, roomy machine
    assert resolved.options.model == "medium"
    auto_meta = resolved.meta["auto"]
    assert auto_meta["chose"] == ["model", "profile"]
    assert "--auto: chose" in auto_meta["explanation"]
    assert resolved.explanations == (auto_meta["explanation"],)


def test_an_explicit_profile_and_model_beat_auto(monkeypatch) -> None:
    monkeypatch.setattr(service_auto, "probe_auto", lambda *a, **k: _probe())
    resolved = service_auto.resolve_run(
        PipelineOptions(model="tiny"), profile="fast", auto=True, directory="/tape"
    )

    assert resolved.options.profile == "fast"
    assert resolved.options.model == "tiny"
    assert resolved.meta["auto"]["chose"] == []


def test_a_multichannel_tape_turns_on_diarization(monkeypatch) -> None:
    monkeypatch.setattr(service_auto, "probe_auto", lambda *a, **k: _probe(channels=4))
    resolved = service_auto.resolve_run(
        PipelineOptions(), profile=None, auto=True, directory="/tape"
    )

    assert resolved.options.do_diarize is True
    assert "diarize" in resolved.meta["auto"]["chose"]


def test_auto_refuses_an_absent_model_without_downloading(monkeypatch) -> None:
    monkeypatch.setattr(
        service_auto, "probe_auto", lambda *a, **k: _probe(models_on_disk=frozenset())
    )
    with pytest.raises(service_auto.ModelNotOnDisk) as err:
        service_auto.resolve_run(
            PipelineOptions(), profile=None, auto=True, directory="/tape"
        )
    assert err.value.model == "medium"


def test_backend_auto_is_orthogonal_to_the_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        service_auto, "available_backend_ids", lambda: ("amd", "nvidia")
    )
    resolved = service_auto.resolve_run(PipelineOptions(backend="auto"), profile="fast")

    assert resolved.options.backend == "nvidia"  # first available in the order
    assert resolved.options.profile == "fast"  # the profile is untouched
    assert resolved.options.best_of == 1
    assert "--backend auto" in resolved.meta["backend_auto"]["explanation"]
    assert resolved.meta["backend_auto"]["backend"] == "nvidia"


def test_backend_auto_refuses_when_nothing_is_available(monkeypatch) -> None:
    monkeypatch.setattr(service_auto, "available_backend_ids", lambda: ())
    with pytest.raises(service_auto.NoBackendAvailable):
        service_auto.resolve_run(PipelineOptions(backend="auto"), profile=None)
