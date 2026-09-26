"""The MCP surface against a registry this build did not write.

The tools read the same registry the console does, so they meet the same two
rows: the one a **released build** left behind (its ``PipelineOptions`` carried
``formats``, which this build does not have) and the one no build wrote. The
first must answer; the second must answer as a *tool error* naming the run and the
field — not as ``Error executing tool <name>``, which is what an uncaught
exception reaches an agent as.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from clear_record.core import PipelineOptions
from clear_record.mcp.server import build_server
from clear_record.service import Registry, RunManager
from mcp_client import error_text as _error_text
from mcp_client import payload as _payload

#: The key the released line wrote and this build does not have (see
#: ``core.options.SUPERSEDED_KEYS``).
_RELEASED_KEY = "formats"


def _registry(tmp_path: Path) -> tuple[Registry, int]:
    """One registry holding one queued run; returns it with that run's id."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor="console",
    )
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(tmp_path),
        actor="console",
    )
    run = registry.create_run(
        meeting.id,
        backend="apple",
        origin="cli",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
        actor="console",
    )
    return registry, run.id


def _server(registry: Registry):
    """A server whose run queue is stopped.

    Built stopped (``start_queue=False``: no drain thread is started), so a
    rescan cannot meet the unreadable row first — that is the drain's own test
    (``tests/service/test_runs.py``) — and this is about what the *tool* answers
    when the read refuses. Asking a running queue to stop would leave the race
    where it is: a pass that has already reached the row cannot be called back.
    """
    return build_server(registry, RunManager(registry, start_queue=False))


def _released_row() -> str:
    """The JSON a released build wrote: this build's options plus its ``formats``."""
    row = dataclasses.asdict(PipelineOptions(backend="apple"))
    row[_RELEASED_KEY] = ["md", "srt"]
    return json.dumps(row)


def _store(registry: Registry, run_id: int, text: str) -> None:
    with closing(sqlite3.connect(str(registry.db_path))) as conn, conn:
        conn.execute(
            "UPDATE pipeline_run SET run_options = ? WHERE id = ?", (text, run_id)
        )


def test_the_server_reads_a_registry_a_release_wrote(tmp_path: Path) -> None:
    """An upgrade serves the registry the release left, runs and all."""
    registry, run_id = _registry(tmp_path)
    _store(registry, run_id, _released_row())
    server = _server(registry)

    runs = _payload(server, "list_runs", {"project": "ops", "meeting": "kickoff"})
    assert [run["id"] for run in runs] == [run_id]
    assert _RELEASED_KEY not in runs[0]["run_options"]

    status = _payload(server, "run_status", {"run_id": run_id})
    assert status["id"] == run_id


def test_a_row_this_build_cannot_read_is_a_tool_error(tmp_path: Path) -> None:
    """The agent is told which run and which field, not that "a tool failed"."""
    registry, run_id = _registry(tmp_path)
    _store(registry, run_id, '{"backend": "apple", "jobs": "many"}')
    server = _server(registry)

    read = _error_text(server, "run_status", {"run_id": run_id})
    assert f"run {run_id}" in read
    assert "jobs" in read

    listed = _error_text(server, "list_runs", {"project": "ops", "meeting": "kickoff"})
    assert f"run {run_id}" in listed
