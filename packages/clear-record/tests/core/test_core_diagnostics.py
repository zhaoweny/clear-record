"""The structured log record and the rotating sink, in ``core``.

The record shape is stable, the level is controllable (``CR_LOG_LEVEL`` or the
programmatic override the CLI's ``--verbose`` uses), and the sink is bounded.
Every surface may write here, so the CLI leaves a trail too.
"""

from __future__ import annotations

import json

import pytest

from clear_record.core import diagnostics as d


@pytest.fixture(autouse=True)
def _reset_level():
    d.set_level(None)
    yield
    d.set_level(None)


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Every test writes logs under its own tmp dir, never the real XDG state."""
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("CR_LOG_LEVEL", raising=False)


def test_record_shape_is_stable_and_scalar() -> None:
    record = d.make_record(
        "info", "runs", "run.started", {"meeting_id": 2, "backend": "apple", "ok": True}
    )
    assert list(record) == [
        "ts",
        "level",
        "component",
        "event",
        "backend",
        "meeting_id",
        "ok",
    ]
    line = d.format_record(record)
    assert "\n" not in line
    assert json.loads(line) == record


def test_level_control_gates_the_sink(monkeypatch) -> None:
    monkeypatch.setenv("CR_LOG_LEVEL", "error")
    assert d.log_event("info", "x", "suppressed", n=1) is None
    assert not d.log_path().exists()

    record = d.log_event("error", "x", "kept", n=1)
    assert record is not None
    lines = d.log_path().read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["kept"]

    monkeypatch.delenv("CR_LOG_LEVEL")
    assert d.effective_level() == "info"
    assert d.enabled("info") is True
    assert d.enabled("debug") is False


def test_set_level_overrides_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CR_LOG_LEVEL", "error")
    d.set_level("debug")
    assert d.effective_level() == "debug"
    assert d.log_event("debug", "x", "verbose", n=1) is not None

    d.set_level(None)
    assert d.effective_level() == "error"


def test_logs_dir_follows_the_precedence(tmp_path, monkeypatch) -> None:
    assert d.logs_dir(tmp_path / "explicit") == tmp_path / "explicit"

    assert d.logs_dir() == tmp_path / "logs"  # the fixture's CR_LOG_DIR

    monkeypatch.delenv("CR_LOG_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert d.logs_dir() == tmp_path / "state" / "clear-record" / "logs"


def test_rotation_keeps_a_bounded_retained_set(tmp_path) -> None:
    path = tmp_path / "logs" / d.LOG_FILENAME
    for index in range(50):
        d.append_line(path, f"line-{index:03d}-{'x' * 20}", max_bytes=200, retained=2)

    names = sorted(candidate.name for candidate in path.parent.iterdir())
    assert names == [
        d.LOG_FILENAME,
        f"{d.LOG_FILENAME}.1",
        f"{d.LOG_FILENAME}.2",
    ]

    # The newest line survived; the oldest fell off the end.
    recent = d.read_recent(1000, explicit=path.parent)
    assert any("line-049" in line for line in recent)
    assert not any("line-000" in line for line in recent)
    assert len(recent) < 50
