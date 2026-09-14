"""The MCP tool surface, exercised through a real in-process MCP client.

The SDK ships an in-process transport (``Client(server)``), so these tests drive
the same stdio code path a user's agent would — list the tools, call them, read
structured results and ``is_error`` failures — with no subprocess and no
network. The pipeline callable is injected, so a full run is started and watched
with no ASR backend and no GPU.

The tool logic itself is a thin translation of ``clear_record.service``; the
service's own behaviour is covered by ``tests/service/``. These tests assert the
*adapter*: the surface exists, results are JSON-serialisable, and failures come
back actionable rather than as crashes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from clear_record.core import Progress
from clear_record.mcp.server import TOOL_NAMES, build_server
from clear_record.service import Registry, RunManager


def _registry(tmp_path: Path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


async def _list_tool_names(server: MCPServer) -> set[str]:
    async with Client(server) as client:
        result = await client.list_tools()
        return {tool.name for tool in result.tools}


async def _call(server: MCPServer, name: str, arguments: dict | None = None):
    async with Client(server) as client:
        return await client.call_tool(name, arguments or {})


def _names(server: MCPServer) -> set[str]:
    return asyncio.run(_list_tool_names(server))


def _result(server: MCPServer, name: str, arguments: dict | None = None):
    return asyncio.run(_call(server, name, arguments))


def _payload(server: MCPServer, name: str, arguments: dict | None = None):
    result = _result(server, name, arguments)
    assert not result.is_error, result.content
    # A tool returning `dict` yields that dict; a tool returning `list[dict]`
    # yields `{"result": [...]}` (the SDK's structured-output convention). Both
    # survive a JSON round-trip, which is what the agent actually reads.
    data = json.loads(json.dumps(result.structured_content))
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def _error_text(server: MCPServer, name: str, arguments: dict | None = None) -> str:
    result = _result(server, name, arguments)
    assert result.is_error, f"{name} unexpectedly succeeded: {result.content}"
    return result.content[0].text


def test_tool_surface_is_registered(tmp_path: Path) -> None:
    server = build_server(_registry(tmp_path))
    assert _names(server) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))  # no accidental duplicate


def test_project_and_glossary_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Weekly Ops", notes="sync")
    server = build_server(registry)

    projects = _payload(server, "list_projects")
    assert [p["slug"] for p in projects] == ["weekly-ops"]
    assert projects[0]["term_count"] == 0

    project = _payload(server, "get_project", {"slug": "weekly-ops"})
    assert project["name"] == "Weekly Ops"
    assert project["notes"] == "sync"

    created = _payload(
        server,
        "add_glossary_term",
        {"project": "weekly-ops", "term": "Falcon", "definition": "the project"},
    )
    assert created["term"] == "Falcon"
    assert created["status"] == "candidate"
    assert created["added_by"] == "human"

    agent_term = _payload(
        server,
        "add_glossary_term",
        {"project": "weekly-ops", "term": "Aero", "added_by": "agent"},
    )
    assert agent_term["added_by"] == "agent"

    terms = _payload(server, "list_glossary_terms", {"project": "weekly-ops"})
    assert {t["term"] for t in terms} == {"Falcon", "Aero"}

    confirmed = _payload(
        server,
        "update_glossary_term",
        {"term_id": created["id"], "status": "confirmed"},
    )
    assert confirmed["status"] == "confirmed"

    filtered = _payload(
        server, "list_glossary_terms", {"project": "weekly-ops", "status": "candidate"}
    )
    assert [t["term"] for t in filtered] == ["Aero"]

    # The glossary changed, so the project's term count reflects it.
    assert _payload(server, "get_project", {"slug": "weekly-ops"})["term_count"] == 2


def test_meeting_tape_set_run_and_artifacts_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")

    def fake_pipeline(directory, options, on_event) -> None:
        assert options.audio_files == (str(tape),)
        progress = Progress("transcribe", 2, on_event)
        progress.start()
        progress.advance(source="a")
        progress.advance(source="a")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")
        (Path(directory) / "record.json").write_text("{}", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)

    meeting = _payload(
        server,
        "create_meeting",
        {"project": "ops", "title": "Kickoff", "workspace_path": str(workspace)},
    )
    assert meeting["slug"] == "kickoff"
    assert meeting["status"] == "new"

    tapes = _payload(
        server,
        "set_meeting_tapes",
        {"project": "ops", "meeting": "kickoff", "paths": [str(tape)]},
    )
    assert tapes["paths"] == [str(tape)]

    read = _payload(server, "get_meeting", {"project": "ops", "meeting": "kickoff"})
    assert read["tapes"] == [str(tape)]
    assert [
        m["slug"] for m in _payload(server, "list_meetings", {"project": "ops"})
    ] == ["kickoff"]

    run = _payload(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    manager.wait(run["id"], timeout=10)

    status = _payload(server, "run_status", {"run_id": run["id"]})
    assert status["status"] == "done"
    assert status["progress"]["stage"] == "transcribe"
    assert status["progress"]["total"] == 2

    events = _payload(server, "run_events", {"run_id": run["id"]})
    assert events["status"] == "done"
    assert len(events["events"]) == 3
    assert events["events"][-1]["done"] is True
    assert events["next"] == 3
    assert (
        _payload(server, "run_events", {"run_id": run["id"], "after": 3})["events"]
        == []
    )

    runs = _payload(server, "list_runs", {"project": "ops", "meeting": "kickoff"})
    assert [r["id"] for r in runs] == [run["id"]]

    artifacts = _payload(
        server, "list_artifacts", {"project": "ops", "meeting": "kickoff"}
    )
    assert {a["kind"] for a in artifacts} == {"record", "export"}
    assert all(a["sha256"] for a in artifacts)


def test_unknown_project_and_meeting_are_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    server = build_server(registry)

    assert "unknown project 'nope'" in _error_text(
        server, "get_project", {"slug": "nope"}
    )
    assert "unknown project 'nope'" in _error_text(
        server, "list_glossary_terms", {"project": "nope"}
    )
    message = _error_text(
        server, "get_meeting", {"project": "ops", "meeting": "missing"}
    )
    assert "unknown meeting 'missing'" in message
    assert "known meetings: (none)" in message


def test_start_run_without_tapes_is_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    server = build_server(registry, RunManager(registry, pipeline=lambda *a: None))

    message = _error_text(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    assert "tape set" in message


def test_backend_failure_is_reported_by_run_status(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])

    def no_backend(directory, options, on_event) -> None:
        raise RuntimeError("backend unavailable")

    manager = RunManager(registry, pipeline=no_backend)
    server = build_server(registry, manager)
    run = _payload(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    manager.wait(run["id"], timeout=10)

    status = _payload(server, "run_status", {"run_id": run["id"]})
    assert status["status"] == "failed"
    assert "backend unavailable" in status["progress"]["error"]


def test_unknown_run_ids_are_actionable(tmp_path: Path) -> None:
    server = build_server(_registry(tmp_path))
    assert "unknown run id 99" in _error_text(server, "run_status", {"run_id": 99})
    assert "run id 99" in _error_text(server, "run_events", {"run_id": 99})
