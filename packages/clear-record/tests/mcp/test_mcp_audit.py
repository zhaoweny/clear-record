"""The stdio adapter's half of the audit record (ADR-0033).

A mutation a harness causes records ``mcp`` — the actor *this transport* supplies
— and never a word the tool's caller chose. The same decision took the ``author``
parameters off the three draft tools: a draft version's author and a decision's
reviewer are the transport's actor, so a harness that decides its own draft over
this adapter is recorded as ``mcp`` — the adapter, which is what the record can
honestly say. A **human's** decision is the console's, and is recorded there.

The tools are driven through the real in-process MCP client, so what is asserted
is the surface an agent sees — including the published input schema, which is
where "no longer accepts an author" is observable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import Client

from clear_record.mcp.server import ServiceTools, build_server
from clear_record.service import CONSOLE, MCP, Registry
from mcp_client import payload as _payload

_MINUTES = {
    "meeting": "Kickoff",
    "project": "Ops",
    "attendees": ["Ada"],
    "decisions": ["ship it"],
    "actions": ["Ada writes the docs"],
    "body": "# Kickoff\n\nWe shipped it.",
}


def _registry(tmp_path: Path) -> Registry:
    """A registry with one meeting that has a workspace, seeded as the console.

    Seeding is the console's work — the meeting is created before any agent is
    involved — so a test can tell the adapter's rows from the ones already there
    by their actor alone.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry.create_project("Ops", actor=CONSOLE)
    registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace), actor=CONSOLE
    )
    return registry


def _schemas(server) -> dict[str, dict]:
    async def collect() -> dict[str, dict]:
        async with Client(server) as client:
            result = await client.list_tools()
            return {tool.name: dict(tool.input_schema) for tool in result.tools}

    return asyncio.run(collect())


def test_the_adapter_records_itself_as_the_actor(tmp_path: Path) -> None:
    """A glossary term added over MCP is one ``mcp`` row, naming the term."""
    registry = _registry(tmp_path)
    tools = ServiceTools(registry)

    tools.add_glossary_term("ops", "Falcon")

    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in registry.list_audit_events()
        if row.actor == MCP
    ] == [(MCP, "term.add", "term:Falcon", "ok")]
    # And what the console seeded is still the console's, not this adapter's.
    assert [row.actor for row in registry.list_audit_events()][:2] == [
        CONSOLE,
        CONSOLE,
    ]


def test_a_harness_written_draft_records_mcp_as_its_author(tmp_path: Path) -> None:
    """The harness's version names ``mcp``; the decision it asks for is not its own.

    The tool takes no author, so the recorded author is the transport's actor —
    which is the whole point: a self-declared identity looks like evidence and is
    not (ADR-0033).
    """
    registry = _registry(tmp_path)
    server = build_server(registry)

    written = _payload(
        server,
        "write_agent_draft",
        {"project": "ops", "meeting": "kickoff", "kind": "minutes", "value": _MINUTES},
    )

    assert written["provenance"]["author"] == MCP
    assert written["versions"][0]["author"] == MCP
    assert [
        (row.actor, row.action, row.target)
        for row in registry.list_audit_events()
        if row.action == "draft.write"
    ] == [(MCP, "draft.write", f"draft:{written['draft_id']}")]


def test_the_draft_tools_publish_no_author_parameter(tmp_path: Path) -> None:
    """What a client is offered: three tools, and no ``author`` among the fields.

    The published schema is the contract an agent reads, so this is where the
    removal is observable — a caller has nothing to declare and nothing to forge.
    """
    schemas = _schemas(build_server(_registry(tmp_path)))

    for name in ("write_agent_draft", "accept_agent_draft", "reject_agent_draft"):
        properties = schemas[name]["properties"]
        assert "author" not in properties, name
    # The fields that carry meaning are still there: a version number to decide,
    # and the kind and value a write needs.
    assert set(schemas["accept_agent_draft"]["properties"]) >= {"version"}
    assert set(schemas["write_agent_draft"]["properties"]) >= {"kind", "value"}


@pytest.mark.parametrize("tool", ["accept_agent_draft", "reject_agent_draft"])
def test_a_decision_over_mcp_is_recorded_against_the_adapter(
    tmp_path: Path, tool: str
) -> None:
    registry = _registry(tmp_path)
    server = build_server(registry)
    written = _payload(
        server,
        "write_agent_draft",
        {"project": "ops", "meeting": "kickoff", "kind": "minutes", "value": _MINUTES},
    )

    decided = _payload(
        server,
        tool,
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": written["draft_id"],
            "version": written["version"],
        },
    )

    expected = "draft.accept" if tool == "accept_agent_draft" else "draft.reject"
    assert decided["versions"][-1]["reviewed_by"] == MCP
    assert [(row.actor, row.action) for row in registry.list_audit_events()][-1] == (
        MCP,
        expected,
    )
