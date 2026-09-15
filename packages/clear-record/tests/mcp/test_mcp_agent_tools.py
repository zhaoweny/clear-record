"""The MCP agent-task tools: launch, read, accept, reject, over the real client.

The adapter is thin, so these assert exactly that: the five tools exist, a launch
returns the draft's structured value, an acceptance returns the promotion's
outcome, and a bad kind or unknown draft comes back as an actionable
``is_error`` rather than a crash. A stub runner keeps it offline (ADR-0018).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.mcp.server import TOOL_NAMES, build_server
from clear_record.service import Registry, Runner, RunnerOutput, RunnerRequest

_ANSWERS: dict[str, dict] = {
    "glossary_collection": {
        "terms": [
            {
                "term": "Falcon",
                "reading": "FAL-kun",
                "aliases": ["falcon"],
                "definition": "a tracked object",
                "evidence": "the falcon is up",
            }
        ]
    },
    "transcript_check": {
        "revision": "00:00:03.000 [mic] the Falcon is up",
        "changes": [{"before": "falcon", "after": "Falcon", "reason": "glossary term"}],
    },
    "minutes": {
        "meeting": "Kickoff",
        "project": "Ops",
        "attendees": ["Ada"],
        "decisions": ["ship it"],
        "actions": ["Ada writes the docs"],
        "body": "# Kickoff\n\nWe shipped it.",
    },
}


class StubRunner(Runner):
    kind = "stub"

    def run(self, request: RunnerRequest) -> RunnerOutput:
        return RunnerOutput(text=json.dumps(_ANSWERS[request.kind]), model="stub-model")


def _registry(tmp_path: Path) -> Registry:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=(
                Segment(start=3.0, end=4.0, text="the falcon is up", source="a"),
            ),
        ),
    )
    registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    return registry


async def _call(server: MCPServer, name: str, arguments: dict | None = None):
    async with Client(server) as client:
        return await client.call_tool(name, arguments or {})


def _payload(server: MCPServer, name: str, arguments: dict | None = None):
    result = asyncio.run(_call(server, name, arguments))
    assert not result.is_error, result.content
    data = json.loads(json.dumps(result.structured_content))
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def _error(server: MCPServer, name: str, arguments: dict | None = None) -> str:
    result = asyncio.run(_call(server, name, arguments))
    assert result.is_error, f"{name} unexpectedly succeeded: {result.content}"
    return result.content[0].text


def _server(tmp_path: Path) -> MCPServer:
    return build_server(_registry(tmp_path), runner=StubRunner())


def test_the_agent_tools_are_registered(tmp_path: Path) -> None:
    names = asyncio.run(_tool_names(_server(tmp_path)))
    for name in (
        "list_agent_drafts",
        "read_agent_draft",
        "run_agent_task",
        "accept_agent_draft",
        "reject_agent_draft",
    ):
        assert name in TOOL_NAMES and name in names


async def _tool_names(server: MCPServer) -> set[str]:
    async with Client(server) as client:
        result = await client.list_tools()
        return {tool.name for tool in result.tools}


def test_list_agent_drafts_reports_the_kinds_and_an_empty_start(
    tmp_path: Path,
) -> None:
    listing = _payload(
        _server(tmp_path),
        "list_agent_drafts",
        {"project": "ops", "meeting": "kickoff"},
    )
    assert listing["tasks"] == [
        "glossary_collection",
        "transcript_check",
        "minutes",
    ]
    assert listing["configured"] is True
    assert listing["drafts"] == []
    assert listing["minutes"] is None


def test_run_read_accept_round_trips_a_draft(tmp_path: Path) -> None:
    server = _server(tmp_path)

    launched = _payload(
        server,
        "run_agent_task",
        {"project": "ops", "meeting": "kickoff", "kind": "glossary_collection"},
    )
    assert launched["review_state"] == "draft"
    assert launched["value"]["terms"][0]["term"] == "Falcon"
    run_id = launched["run_id"]

    read = _payload(
        server,
        "read_agent_draft",
        {"project": "ops", "meeting": "kickoff", "run_id": run_id},
    )
    assert read["value"] == launched["value"]
    assert read["provenance"]["runner"] == "stub"

    accepted = _payload(
        server,
        "accept_agent_draft",
        {"project": "ops", "meeting": "kickoff", "run_id": run_id},
    )
    assert accepted["review_state"] == "accepted"
    assert accepted["promotion"]["summary"]["added"] == ["Falcon"]


def test_accepting_minutes_registers_the_meeting_minutes(tmp_path: Path) -> None:
    server = _server(tmp_path)
    launched = _payload(
        server,
        "run_agent_task",
        {"project": "ops", "meeting": "kickoff", "kind": "minutes"},
    )
    accepted = _payload(
        server,
        "accept_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "run_id": launched["run_id"],
        },
    )
    assert accepted["promotion"]["kind"] == "minutes"
    listing = _payload(
        server, "list_agent_drafts", {"project": "ops", "meeting": "kickoff"}
    )
    assert listing["minutes"]["kind"] == "minutes"
    assert listing["minutes"]["produced_by"] == "agent"


def test_rejecting_a_draft_keeps_it(tmp_path: Path) -> None:
    server = _server(tmp_path)
    launched = _payload(
        server,
        "run_agent_task",
        {"project": "ops", "meeting": "kickoff", "kind": "minutes"},
    )
    rejected = _payload(
        server,
        "reject_agent_draft",
        {"project": "ops", "meeting": "kickoff", "run_id": launched["run_id"]},
    )
    assert rejected["review_state"] == "rejected"
    assert rejected["promotion"] is None


def test_an_unknown_kind_and_an_unknown_draft_are_actionable_errors(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    text = _error(
        server,
        "run_agent_task",
        {"project": "ops", "meeting": "kickoff", "kind": "summarize"},
    )
    assert "unknown agent task kind" in text

    text = _error(
        server,
        "read_agent_draft",
        {"project": "ops", "meeting": "kickoff", "run_id": "nope"},
    )
    assert "unknown agent draft" in text
