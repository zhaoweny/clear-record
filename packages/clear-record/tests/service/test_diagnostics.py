"""The redacted diagnostics bundle (assembly, redaction, opt-in, the handler).

The log record shape and the rotating sink live in
``clear_record.core.diagnostics`` and are tested there; this module owns the
bundle a user hands us. It is **safe by default**: a redaction test fails if a
transcript, a glossary term, an audio file or a private basename can appear; an
opt-in test proves the private path works when the user asks for it.
"""

from __future__ import annotations

import argparse
import json

import pytest

from clear_record.core import read_recent
from clear_record.service import Registry, RunManager, diagnostics


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Every test writes logs under its own tmp dir, never the real XDG state."""
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("CR_LOG_LEVEL", raising=False)


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _meeting_with_tapes(registry: Registry, tmp_path):
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    registry.set_recording_set(meeting.id, [str(tape)])
    return meeting


# --- redaction ------------------------------------------------------------ #


def test_hashes_are_stable_and_keep_the_path_shape() -> None:
    assert diagnostics.hash_component("take1.wav") == diagnostics.hash_component(
        "take1.wav"
    )
    assert diagnostics.hash_component("take1.wav") != diagnostics.hash_component(
        "take2.wav"
    )
    assert diagnostics.hash_component("take1.wav").endswith(".wav")
    assert diagnostics.hash_component(".env").startswith(".")
    assert diagnostics.hash_component("") == ""

    redacted = diagnostics.redact_path("/Users/alice/rec/take1.wav")
    assert redacted.count("/") == 4  # shape: separators preserved
    assert "alice" not in redacted
    assert "take1" not in redacted
    assert redacted.endswith(".wav")
    assert diagnostics.redact_path(r"C:\Users\bob\a.wav").startswith("C:\\")


def test_redact_text_covers_paths_and_bare_filenames() -> None:
    redacted = diagnostics.redact_text(
        "source /home/bob/secret take1.wav and notes.txt"
    )
    assert "bob" not in redacted
    assert "secret" not in redacted
    assert "take1" not in redacted
    assert "notes" not in redacted
    assert ".wav" in redacted and ".txt" in redacted


def test_bundle_redacts_private_material(tmp_path) -> None:
    private_dir = "/home/alice/PRIVATE-MEETING"
    private_file = "SECRET-TAKE.wav"
    facts = diagnostics.BundleFacts(
        version="0.2.0.dev0",
        python="3.14.0",
        platform="TestOS",
        machine="x86_64",
        backends={"apple": {"available": False, "reason": "requires Darwin"}},
        options={
            "backend": "apple",
            "model": "ggml-base.bin",
            "glossary": f"{private_dir}/glossary.txt",
        },
        run={
            "run_id": 1,
            "status": "failed",
            "error": f"RuntimeError: could not read {private_dir}/{private_file}",
        },
        workspace=private_dir,
        log_lines=[
            json.dumps(
                {
                    "ts": "2026-09-15T00:00:00+00:00",
                    "level": "info",
                    "component": "runs",
                    "event": "run.failed",
                    "error": f"{private_dir}/{private_file}",
                    "note": f"opening {private_file}",
                }
            )
        ],
        include_private=False,
    )
    text = diagnostics.build_bundle(facts)

    assert "PRIVATE-MEETING" not in text
    assert "SECRET-TAKE.wav" not in text
    assert "glossary.txt" not in text
    assert private_dir not in text
    # The public model name is not a user file: it stays readable for triage.
    assert "ggml-base.bin" in text
    # Stable hashes stand in for the identity.
    assert diagnostics.hash_component(private_file) in text
    assert diagnostics.hash_component("PRIVATE-MEETING") in text
    assert "NOT TELEMETRY" in text


def test_collection_never_reads_transcript_audio_or_glossary_by_default(
    tmp_path,
) -> None:
    workspace = tmp_path / "PRIVATE-PROJECT-ALICE"
    workspace.mkdir()
    (workspace / "transcribe.log").write_text(
        "transcribing PRIVATE-TAKE-1.wav\n", encoding="utf-8"
    )
    (workspace / "segments.json").write_text(
        json.dumps(
            {"sources": {"PRIVATE-TAKE-1.wav": [{"text": "PINAPPLE-SECRET-PHRASE"}]}}
        ),
        encoding="utf-8",
    )
    (workspace / "glossary.txt").write_text(
        "PINAPPLE-GLOSSARY-TERM\n", encoding="utf-8"
    )
    (workspace / "PRIVATE-TAKE-1.wav").write_bytes(b"AUDIO-BYTES-MARKER")

    text = diagnostics.collect_bundle(workspace=str(workspace), include_private=False)

    assert "PINAPPLE-SECRET-PHRASE" not in text  # no transcript text
    assert "PINAPPLE-GLOSSARY-TERM" not in text  # no glossary terms
    assert "PRIVATE-TAKE-1.wav" not in text  # no basenames
    assert "AUDIO-BYTES-MARKER" not in text  # no audio
    assert "PRIVATE-PROJECT-ALICE" not in text  # no directory names
    assert diagnostics.hash_component("PRIVATE-PROJECT-ALICE") in text  # shape kept
    assert "# withheld" in text


def test_include_private_adds_the_private_detail(tmp_path) -> None:
    workspace = tmp_path / "PRIVATE-PROJECT-ALICE"
    workspace.mkdir()
    (workspace / "transcribe.log").write_text(
        "transcribing PRIVATE-TAKE-1.wav\n", encoding="utf-8"
    )
    (workspace / "segments.json").write_text(
        json.dumps(
            {"sources": {"PRIVATE-TAKE-1.wav": [{"text": "PINAPPLE-SECRET-PHRASE"}]}}
        ),
        encoding="utf-8",
    )
    (workspace / "glossary.txt").write_text(
        "PINAPPLE-GLOSSARY-TERM\n", encoding="utf-8"
    )
    (workspace / "PRIVATE-TAKE-1.wav").write_bytes(b"AUDIO-BYTES-MARKER")

    text = diagnostics.collect_bundle(workspace=str(workspace), include_private=True)

    assert "PINAPPLE-SECRET-PHRASE" in text
    assert "PINAPPLE-GLOSSARY-TERM" in text
    assert "PRIVATE-TAKE-1.wav" in text
    assert "PRIVATE-PROJECT-ALICE" in text
    assert "AUDIO-BYTES-MARKER" not in text  # audio is never read, even opt-in
    assert "nothing further" in text


# --- run lifecycle logging ------------------------------------------------ #


def test_run_lifecycle_is_logged(tmp_path) -> None:
    registry = _registry(tmp_path)
    meeting = _meeting_with_tapes(registry, tmp_path)

    manager = RunManager(registry, pipeline=lambda *args: None)
    run = manager.start(meeting)
    manager.wait(run.id, timeout=10)

    events = [json.loads(line) for line in read_recent(100)]
    assert "run.started" in [event["event"] for event in events]
    assert "run.finished" in [event["event"] for event in events]
    started = next(event for event in events if event["event"] == "run.started")
    assert started["backend"] == "apple"
    assert started["run_id"] == run.id


def test_a_failing_run_logs_the_reason(tmp_path) -> None:
    registry = _registry(tmp_path)
    meeting = _meeting_with_tapes(registry, tmp_path)

    def boom(*args) -> None:
        raise RuntimeError("backend exploded")

    manager = RunManager(registry, pipeline=boom)
    run = manager.start(meeting)
    manager.wait(run.id, timeout=10)

    failures = [json.loads(line) for line in read_recent(100) if '"run.failed"' in line]
    assert failures, "a failing run must be logged"
    assert "backend exploded" in failures[-1]["error"]


def test_a_refused_start_is_logged(tmp_path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    meeting = registry.create_meeting("ops", "Empty", workspace_path=str(workspace))

    manager = RunManager(registry, pipeline=lambda *args: None)
    with pytest.raises(ValueError):
        manager.start(meeting)

    events = [json.loads(line)["event"] for line in read_recent(100)]
    assert "run.refused" in events


def test_startup_reconciliation_is_logged(tmp_path) -> None:
    """A run left running by a dead process is logged as interrupted."""
    from clear_record.service import RESTART_REASON

    registry = _registry(tmp_path)
    meeting = _meeting_with_tapes(registry, tmp_path)
    orphan = registry.create_run(meeting.id, backend="apple")
    registry.update_run(orphan.id, status="running", started_at="now")

    RunManager(registry, pipeline=lambda *args: None)

    events = [json.loads(line) for line in read_recent(100)]
    interrupted = next(event for event in events if event["event"] == "run.interrupted")
    assert interrupted["run_id"] == orphan.id
    assert interrupted["reason"] == RESTART_REASON


# --- console (uvicorn) logging into the sink ------------------------------ #


def test_console_log_config_routes_uvicorn_to_the_sink(tmp_path) -> None:
    """`serve`'s log config carries uvicorn's records into the diagnostics sink."""
    import logging

    config = diagnostics.console_log_config()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["handlers"] == ["diagnostics"]
        assert config["loggers"][name]["propagate"] is False

    handler = diagnostics.DiagnosticsHandler()
    handler.handle(
        logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '127.0.0.1 - "GET /api/health HTTP/1.1" 200',
            None,
            None,
        )
    )
    handler.handle(
        logging.LogRecord(
            "uvicorn.error", logging.WARNING, __file__, 2, "a warning", None, None
        )
    )

    records = [json.loads(line) for line in read_recent(100)]
    console_records = [record for record in records if record["event"] == "console.log"]
    assert [record["component"] for record in console_records] == ["console", "console"]
    assert console_records[0]["level"] == "info"
    assert "GET /api/health" in console_records[0]["message"]
    assert console_records[1]["level"] == "warning"


# --- the CLI handler ------------------------------------------------------ #


def _args(**overrides) -> argparse.Namespace:
    base = {
        "dry_run": False,
        "include_private": False,
        "run_id": None,
        "meeting_id": None,
        "output": None,
        "data_dir": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_diagnose_dry_run_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert diagnostics.run_diagnose(_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "NOT TELEMETRY" in out
    assert "dry run" in out
    assert not (tmp_path / diagnostics.BUNDLE_FILENAME).exists()


def test_diagnose_writes_and_says_what_it_contains(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    assert diagnostics.run_diagnose(_args()) == 0
    target = tmp_path / diagnostics.BUNDLE_FILENAME
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "NOT TELEMETRY" in body
    assert "# withheld" in body
    out = capsys.readouterr().out
    assert str(target) in out
    assert "NOT telemetry" in out


def test_include_private_prints_exactly_what_it_adds(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    diagnostics.run_diagnose(_args(dry_run=True, include_private=True))
    out = capsys.readouterr().out
    assert "ADDS" in out
    assert "real file paths" in out
    assert "transcript" in out
    assert "workspace log" in out
