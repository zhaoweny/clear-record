"""The agent test-drive script's BYOK rules, at the surface only.

"just agent-drive" is optional and never part of verify or e2e, but the two
rules that live only in the script - resolve the key without ever printing it,
and skip cleanly when there is none - are worth a red test. The drive itself is
run by the operator against a real endpoint; nothing here touches the network.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
import wave
from pathlib import Path

import pytest

from clear_record.service import Registry
from clear_record.service.transcript import read_transcript

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVE_PATH = REPO_ROOT / "scripts" / "agent_drive.py"


@pytest.fixture
def drive() -> types.ModuleType:
    """The script, loaded as a module (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("agent_drive", DRIVE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the class module up in sys.modules while the decorator
    # runs, so the module must be registered before execution.
    sys.modules["agent_drive"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("agent_drive", None)


# --- key resolution: environment first, file second, default last ---------- #


def test_key_comes_from_the_environment(drive) -> None:
    resolution = drive.resolve_key({drive.ENV_KEY: "sk-env-secret"})
    assert resolution.key == "sk-env-secret"
    assert drive.ENV_KEY in resolution.source


def test_key_comes_from_the_named_file(drive, tmp_path: Path) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-file-secret\n", encoding="utf-8")

    resolution = drive.resolve_key({drive.ENV_KEY_FILE: str(key_file)})

    assert resolution.key == "sk-file-secret"
    assert str(key_file) in resolution.source


def test_the_environment_wins_over_the_named_file(drive, tmp_path: Path) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-file-secret", encoding="utf-8")

    resolution = drive.resolve_key(
        {drive.ENV_KEY: "sk-env-secret", drive.ENV_KEY_FILE: str(key_file)}
    )

    assert resolution.key == "sk-env-secret"


def test_a_named_file_that_is_missing_is_reported_not_raised(drive) -> None:
    resolution = drive.resolve_key({drive.ENV_KEY_FILE: "/nope/missing-key.txt"})

    assert resolution.key is None
    assert "missing or empty" in resolution.detail


def test_missing_key_is_a_skip_not_a_crash(drive) -> None:
    resolution = drive.resolve_key({}, candidates=[])

    assert resolution.key is None
    assert drive.ENV_KEY in resolution.detail


def test_main_without_a_key_returns_zero_and_says_skip(
    drive, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(drive, "key_file_candidates", lambda: [])
    monkeypatch.delenv(drive.ENV_KEY, raising=False)
    monkeypatch.delenv(drive.ENV_KEY_FILE, raising=False)

    assert drive.main([]) == 0

    out = capsys.readouterr().out
    assert "skip" in out.lower()
    assert drive.ENV_KEY in out


# --- redaction: the key value never reaches a line ------------------------- #


def test_redactor_masks_the_value_it_was_given(drive) -> None:
    redact = drive.Redactor(("sk-live-secret-value",))

    rendered = redact("token=sk-live-secret-value end")

    assert "sk-live-secret-value" not in rendered
    assert "token=*** end" == rendered


def test_redactor_masks_unknown_sk_shaped_tokens(drive) -> None:
    redact = drive.Redactor(("sk-known-secret",))

    rendered = redact("leaked sk-abcdef123456 here")

    assert "sk-abcdef123456" not in rendered
    assert "sk-***" in rendered


# --- the throwaway seed dir guard ------------------------------------------ #


def test_prepare_data_dir_refuses_an_unmarked_directory(drive, tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir()
    keep = target / "important.txt"
    keep.write_text("do not lose me", encoding="utf-8")

    with pytest.raises(drive.DriveError):
        drive.prepare_data_dir(target)

    assert keep.read_text(encoding="utf-8") == "do not lose me"


def test_prepare_data_dir_recreates_a_marked_directory(drive, tmp_path: Path) -> None:
    target = tmp_path / "data"
    target.mkdir()
    (target / drive.SEED_MARKER).write_text("seed", encoding="utf-8")
    (target / "old.sqlite3").write_text("stale", encoding="utf-8")

    out = drive.prepare_data_dir(target)

    assert out == target.resolve()
    assert not (target / "old.sqlite3").exists()
    assert (target / drive.SEED_MARKER).is_file()


# --- the seed is the e2e pattern: a meeting with a readable transcript ------ #


def test_seed_writes_a_meeting_with_a_transcript(drive, tmp_path: Path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")

    meeting = drive.seed(registry, tmp_path / "workspaces")

    workspace = Path(meeting.workspace_path)
    assert (workspace / "record.json").is_file()
    assert read_transcript(meeting).text.strip()
    assert registry.get_project("agent-drive") is not None


# --- the tape source: a bad tape is reported, never a crash ---------------- #


def test_a_missing_tape_file_fails_the_leg(drive, tmp_path: Path) -> None:
    report = drive.Report(drive.Redactor())
    args = argparse.Namespace(tape=str(tmp_path / "nope.wav"), lang="en")

    source = drive.drive_tape(object(), object(), tmp_path, args, report)

    assert source == "seeded"
    assert any(story.status == "FAIL" for story in report.stories)


def test_a_zero_frame_tape_is_a_finding_not_a_crash(drive, tmp_path: Path) -> None:
    tape = tmp_path / "silent.wav"
    with wave.open(str(tape), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"")
    report = drive.Report(drive.Redactor())
    args = argparse.Namespace(tape=str(tape), lang="en")

    source = drive.drive_tape(object(), object(), tmp_path, args, report)

    assert source == "seeded"
    assert any(story.status == "FINDING" for story in report.stories)
