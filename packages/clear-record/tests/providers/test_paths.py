"""The single models-directory resolver (ADR-0007, ADR-0025).

Precedence: explicit flag -> ``CR_MODELS_DIR`` -> config ``models_dir`` ->
``<data>/models`` (platform-native), with the old ``<cwd>/models`` adopted in
place when the recognizer this layer supplies identifies a cache in it and
nothing overrides. The CLI and the provider both consult the one function in
:func:`clear_record.providers.paths.resolve_models_dir`, so these tests own the
rule — and, because the positive identification is the *provider's* knowledge
(:mod:`clear_record.providers.model_cache`), the legacy-adoption cases live here
too, next to the artifacts they recognize.
"""

from __future__ import annotations

from pathlib import Path

from clear_record.core import paths
from clear_record.providers.paths import resolve_models_dir


def _legacy_cwd_cache(tmp_path: Path) -> Path:
    """A fake legacy ``<cwd>/models`` holding one completed model download."""
    cache = tmp_path / "models"
    cache.mkdir()
    (cache / "ggml-small.bin").write_bytes(b"weights")
    return cache


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


# --- the legacy <cwd>/models adoption, and the recognizer behind it --------- #
def test_the_legacy_cwd_models_is_adopted_through_the_provider_seam(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    legacy = _legacy_cwd_cache(tmp_path)
    assert resolve_models_dir() == str(legacy)

    notice = capsys.readouterr().err
    assert "adopting" in notice
    assert str(legacy) in notice
    assert str(tmp_path / "app" / "data" / "models") in notice
    assert "CR_MODELS_DIR" in notice


def test_the_recognizer_is_installed_into_the_core_resolver(
    tmp_path, monkeypatch
) -> None:
    """The service, the console and the MCP server resolve through ``core.paths``.

    Only the CLI, the pipeline layer's resolver and the backend go through this
    module's own wrapper, so what keeps those other entry points adopting a
    pre-move ``<cwd>/models`` is the import that installs the recognizer (which
    importing this layer performs).
    """
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    legacy = _legacy_cwd_cache(tmp_path)
    assert paths.resolve_models_dir() == legacy


def test_the_adoption_notice_is_printed_once(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    _legacy_cwd_cache(tmp_path)
    resolve_models_dir()
    assert "adopting" in capsys.readouterr().err
    # Adopting the same location again says nothing new.
    resolve_models_dir()
    assert capsys.readouterr().err == ""


def test_the_native_models_dir_wins_when_it_exists(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    _legacy_cwd_cache(tmp_path)
    (tmp_path / "app" / "data" / "models").mkdir(parents=True)
    assert resolve_models_dir() == str(tmp_path / "app" / "data" / "models")
    assert "adopting" not in capsys.readouterr().err


def test_a_cwd_models_that_is_not_a_model_cache_is_left_alone(
    tmp_path, monkeypatch, capsys
) -> None:
    """``<cwd>`` is wherever the command ran, so a shared name proves nothing."""
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "README.md").write_text("not weights")
    assert resolve_models_dir() == str(tmp_path / "app" / "data" / "models")
    assert "adopting" not in capsys.readouterr().err


def test_a_cwd_models_with_only_a_partial_download_is_left_alone(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CR_MODELS_DIR", raising=False)
    monkeypatch.setattr(paths, "_cwd", lambda: tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ggml-small.bin.4242.0.part").write_bytes(b"half")
    assert resolve_models_dir() == str(tmp_path / "app" / "data" / "models")
    assert "adopting" not in capsys.readouterr().err
