"""The MCP draft tools: write, read, accept, reject, over the real client.

The adapter is thin, so these assert exactly that: the five tools exist, a write
returns the chain's structured value with the actor this transport recorded, a
new version goes on the same chain, an acceptance returns the promotion's outcome
and records who decided it, and a bad kind or unknown draft comes back as an
actionable ``is_error`` rather than a crash. No model, endpoint or key is
involved (ADR-0031): the harness writes, the app stores.
"""

from __future__ import annotations

from pathlib import Path

from clear_record.mcp.server import TOOL_NAMES, build_server
from clear_record.service import MCP, Registry
from mcp_client import error_text as _error
from mcp_client import names as _names
from mcp_client import payload as _payload

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


def _registry(tmp_path: Path) -> Registry:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project(
        "Ops",
        actor="console",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(workspace),
        actor="console",
    )
    return registry


def _server(tmp_path: Path):
    return build_server(_registry(tmp_path))


def _write(server, kind: str, *, draft_id=None):
    arguments = {
        "project": "ops",
        "meeting": "kickoff",
        "kind": kind,
        "value": _ANSWERS[kind],
    }
    if draft_id is not None:
        arguments["draft_id"] = draft_id
    return _payload(server, "write_agent_draft", arguments)


def test_the_agent_tools_are_registered(tmp_path: Path) -> None:
    names = _names(_server(tmp_path))
    for name in (
        "list_agent_drafts",
        "read_agent_draft",
        "write_agent_draft",
        "accept_agent_draft",
        "reject_agent_draft",
    ):
        assert name in TOOL_NAMES and name in names


def test_list_agent_drafts_reports_the_kinds_and_an_empty_start(
    tmp_path: Path,
) -> None:
    listing = _payload(
        _server(tmp_path),
        "list_agent_drafts",
        {"project": "ops", "meeting": "kickoff"},
    )
    assert listing["kinds"] == [
        "glossary_collection",
        "transcript_check",
        "minutes",
    ]
    assert listing["drafts"] == []
    assert listing["minutes"] is None


def test_write_read_accept_round_trips_a_draft(tmp_path: Path) -> None:
    server = _server(tmp_path)

    written = _write(server, "glossary_collection")
    assert written["review_state"] == "draft"
    assert written["value"]["terms"][0]["term"] == "Falcon"
    assert written["provenance"]["author"] == MCP
    draft_id = written["draft_id"]

    read = _payload(
        server,
        "read_agent_draft",
        {"project": "ops", "meeting": "kickoff", "draft_id": draft_id},
    )
    assert read["value"] == written["value"]
    assert read["provenance"]["author"] == MCP
    assert read["versions"] == written["versions"]

    accepted = _payload(
        server,
        "accept_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": draft_id,
            "version": written["version"],
        },
    )
    assert accepted["review_state"] == "accepted"
    assert accepted["promotion"]["summary"]["added"] == ["Falcon"]
    # Who wrote it and who decided it are both on the chain — the same actor, the
    # adapter's, because both tools were called over this transport and neither
    # takes an identity from its caller (ADR-0033).
    assert accepted["versions"][0]["author"] == MCP
    assert accepted["versions"][0]["reviewed_by"] == MCP


def test_a_second_write_appends_a_version_recorded_against_its_writer(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    first = _write(server, "minutes")

    second = _write(server, "minutes", draft_id=first["draft_id"])

    assert [version["author"] for version in second["versions"]] == [MCP, MCP]
    # A new version puts the draft back in review.
    assert second["review_state"] == "draft"
    listing = _payload(
        server, "list_agent_drafts", {"project": "ops", "meeting": "kickoff"}
    )
    assert len(listing["drafts"]) == 1


def test_accepting_minutes_registers_the_meeting_minutes(tmp_path: Path) -> None:
    server = _server(tmp_path)
    written = _write(server, "minutes")
    accepted = _payload(
        server,
        "accept_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": written["draft_id"],
            "version": written["version"],
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
    written = _write(server, "minutes")
    rejected = _payload(
        server,
        "reject_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": written["draft_id"],
            "version": written["version"],
        },
    )
    assert rejected["review_state"] == "rejected"
    assert rejected["promotion"] is None
    assert rejected["versions"][-1]["reviewed_by"] == MCP


def test_the_decision_tools_require_the_version_they_decide(tmp_path: Path) -> None:
    """A decision names its version: no surface defaults to the newest.

    The schema is the contract a harness reads, so the required field is what
    stops a decision from being applied to a version nobody named — the caller
    reads a draft, then decides *that* version.
    """
    server = _server(tmp_path)
    written = _write(server, "minutes")

    for tool in ("accept_agent_draft", "reject_agent_draft"):
        message = _error(
            server,
            tool,
            {"project": "ops", "meeting": "kickoff", "draft_id": written["draft_id"]},
        )
        assert "version" in message and "required" in message

    # And the version it names is the one that gets decided.
    listing = _payload(
        server, "list_agent_drafts", {"project": "ops", "meeting": "kickoff"}
    )
    assert [draft["review_state"] for draft in listing["drafts"]] == ["draft"]


def test_a_payload_that_cannot_be_its_kind_is_refused_over_mcp(
    tmp_path: Path,
) -> None:
    """The refusal is actionable: it names the key that is missing."""
    server = _server(tmp_path)

    text = _error(
        server,
        "write_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "kind": "minutes",
            "value": {},
        },
    )

    assert "minutes" in text and "body" in text
    listing = _payload(
        server, "list_agent_drafts", {"project": "ops", "meeting": "kickoff"}
    )
    assert listing["drafts"] == []


def test_a_decision_naming_a_stale_version_is_refused_over_mcp(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    first = _write(server, "minutes")
    _write(server, "minutes", draft_id=first["draft_id"])

    text = _error(
        server,
        "accept_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": first["draft_id"],
            "version": 1,
        },
    )
    assert "not the newest" in text

    accepted = _payload(
        server,
        "accept_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "draft_id": first["draft_id"],
            "version": 2,
        },
    )
    assert accepted["review_state"] == "accepted"
    assert accepted["versions"][1]["reviewed_by"] == MCP


def test_an_unknown_kind_and_an_unknown_draft_are_actionable_errors(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    text = _error(
        server,
        "write_agent_draft",
        {
            "project": "ops",
            "meeting": "kickoff",
            "kind": "summarize",
            "value": {},
        },
    )
    # The service's own message carries the kind and the known kinds; the
    # adapter does not restate it.
    assert "unknown draft kind" in text
    assert "summarize" in text
    assert "glossary_collection" in text

    text = _error(
        server,
        "read_agent_draft",
        {"project": "ops", "meeting": "kickoff", "draft_id": "nope"},
    )
    assert "unknown agent draft" in text
