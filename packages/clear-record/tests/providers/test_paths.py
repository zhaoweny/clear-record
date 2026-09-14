"""The single models-directory resolver (ADR-0007, ADR-0025).

Precedence: explicit flag -> ``CR_MODELS_DIR`` -> config ``models_dir`` ->
``<data>/models`` (platform-native), with the old ``<cwd>/models`` adopted in
place when it looks like a ggml cache and nothing overrides. The CLI and the
provider both consult this one function, so these tests own the rule.
"""

from __future__ import annotations

from clear_record.core import paths
from clear_record.providers.paths import resolve_models_dir


def test_flag_wins_over_env_and_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path / "env"))
    assert resolve_models_dir(str(tmp_path / "flag")) == str(tmp_path / "flag")


def test_env_wins_over_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path / "env"))
    monkeypatch.chdir(tmp_path)
    assert resolve_models_dir() == str(tmp_path / "env")
    assert resolve_models_dir(None) == str(tmp_path / "env")


def test_default_is_models_under_the_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_models_dir() == str(tmp_path / "app" / "data" / "models")


def test_the_legacy_cwd_models_is_adopted_through_the_provider_seam(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    legacy = tmp_path / "models"
    legacy.mkdir()
    (legacy / "ggml-small.bin").write_bytes(b"weights")
    assert resolve_models_dir() == str(legacy)
    assert "adopting" in capsys.readouterr().err


def test_a_cwd_models_that_is_not_a_ggml_cache_is_left_alone(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    (tmp_path / "models").mkdir()
    assert resolve_models_dir() == str(tmp_path / "app" / "data" / "models")
    assert "adopting" not in capsys.readouterr().err
