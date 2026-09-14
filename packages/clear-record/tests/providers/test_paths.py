"""The single models-directory resolver (ADR-0007, ADR-0025).

Precedence: explicit flag -> ``CR_MODELS_DIR`` -> config ``models_dir`` ->
``<data>/models`` (platform-native). The CLI and the provider both consult this
one function, so these tests own the rule.
"""

from __future__ import annotations

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
