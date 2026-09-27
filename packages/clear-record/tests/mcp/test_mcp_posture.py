"""The adapter's posture toward the node, stated for both cases.

An MCP surface's reader is a harness, not a person: what an agent knows about the
node is what these instructions state, and nothing it did not read. So this is
where the adapter says what it does — a node running, and none — and this file is
also the guard that the statement stays true of the code: the adapter asks the
node whether it is *there* and starts none, so it must have no path that could
start one (ADR-0032 keeps MCP in process over ``clear_record.service``;
ADR-0017's transport is unchanged).
"""

from __future__ import annotations

import ast
import http.server
import threading
from pathlib import Path

import pytest

from clear_record.core import node
from clear_record.mcp import server as mcp_server
from clear_record.mcp.server import build_server, node_line
from clear_record.service import Registry

SOURCE = Path(mcp_server.__file__)


class _Node(http.server.BaseHTTPRequestHandler):
    """A listener that answers the node's health path exactly as a node does."""

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep the test output quiet


@pytest.fixture
def answering_node():
    """A node already listening where the record says: the case the adapter states.

    A listener that answers the health path is all this file needs of one — the
    record, one request against it, one answer. The real ``serve`` posture is
    driven in ``tests/test_node_address.py``; the tray's client half, in
    ``tests/tray/test_service.py``.
    """
    listener = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Node)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    address = node.NodeAddress.of("127.0.0.1", listener.server_address[1])
    node.record(address)
    try:
        yield address
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(5)
        node.forget()


def _instructions(tmp_path: Path) -> str:
    """The instructions ``main`` hands the agent: the adapter's text and the case."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    return build_server(registry, node_note=node_line()).instructions


def _stated_posture(instructions: str) -> None:
    """The posture itself, which is the same in both cases: these tools answer with
    or without a node, and this adapter starts none."""
    assert "answer the same either way" in instructions
    assert "never starts a node" in instructions


def test_with_no_node_the_posture_and_the_one_sentence_are_both_stated(
    tmp_path: Path,
) -> None:
    """No node is not "these tools do not work" — so the posture is stated with it.

    The absence case is the one an agent is likeliest to misread, because the
    sentence every surface states reads like a refusal: the instructions therefore
    carry what that sentence means for these tools.
    """
    instructions = _instructions(tmp_path)

    assert node.NO_NODE_MESSAGE in instructions
    _stated_posture(instructions)


def test_with_a_node_the_posture_and_the_address_are_both_stated(
    tmp_path: Path, answering_node: node.NodeAddress
) -> None:
    """With a node listening: the same posture, and which node this is."""
    instructions = _instructions(tmp_path)

    assert f"The clear-record node is listening at {answering_node.url}" in instructions
    _stated_posture(instructions)


#: The names a node is *started* with, or whose record is cleared: a process
#: behind it, a server run in this process, or its address written where the
#: surfaces resolve it. Asking whether a node is there (``node.ask``) is not one.
_STARTS_A_NODE = frozenset(
    {"subprocess", "Popen", "uvicorn", "NodeServer", "record", "forget"}
)


def _name_texts(parsed: ast.AST) -> list[str]:
    """The name-like texts one node contributes: identifiers, attributes, aliases."""
    if isinstance(parsed, ast.Name):
        return [parsed.id]
    if isinstance(parsed, ast.Attribute):
        return [parsed.attr]
    if isinstance(parsed, ast.alias):
        return [part for part in (parsed.name, parsed.asname) if part]
    if isinstance(parsed, ast.keyword):
        return [parsed.arg] if parsed.arg else []
    return []


def test_the_adapter_has_no_path_that_starts_a_node() -> None:
    """The posture's promise is a property of the module, not a word to an agent.

    What would make the stated posture false is a path that spawns a node, runs one
    here, or writes the address a surface resolves. No *name* in the module is one
    of the things a node is started with; docstrings may describe nodes, because
    prose is not a code path.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    found = sorted(
        f"{SOURCE.name}:{parsed.lineno}: {name}"
        for parsed in ast.walk(tree)
        for name in _name_texts(parsed)
        if name in _STARTS_A_NODE
    )
    assert not found, (
        "clear_record.mcp.server must stay a client of the service in process, "
        "with no path that starts a node (ADR-0032):\n" + "\n".join(found)
    )
