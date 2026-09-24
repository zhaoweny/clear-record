"""The node's address is recorded, and every surface finds it.

The relation these tests prove is not "some modules import :mod:`urllib`". It is:

- a node started in **any** of its postures records where it is, where every
  surface resolves it, and the port it records is the one its socket *bound*
  (a ``--port 0`` node does not record the 0 it asked for);
- a **second process** reads that address and completes a request against the
  node;
- no surface scans for a port: a record pointing somewhere nothing answers is a
  refusal, not the start of a search, even while a node listens elsewhere on the
  machine;
- an **absent or stale** address yields one stated error, the same sentence from
  the command line, the console and the MCP adapter;
- the port has a single declaration (:data:`clear_record.core.node.DEFAULT_PORT`).

The console is asked through ``GET /api/node``; the MCP adapter through the note
it hands its instructions. Both surface entry points are exercised with nothing
listening and with a node running, because "the same answer from every surface"
is only meaningful if the same surfaces are asked both ways.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clear_record.cli import cli
from clear_record.core import node, paths
from clear_record.mcp.server import node_line
from clear_record.service import Registry
from clear_record.tray.service import ServiceController
from clear_record.web import app as web
from clear_record.web.app import create_app

#: The child process's whole program: read the node's address and complete one
#: request against it, through the same client and the same verb a person runs.
_CHILD = "from clear_record.cli.cli import main\nraise SystemExit(main(['node']))\n"

#: How long a real node gets to record its address before a test calls it a bug.
_READY_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_record(timeout: float = _READY_TIMEOUT) -> node.NodeAddress:
    """The address the node under test recorded, or a failure naming the wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recorded = node.recorded()
        if recorded is not None:
            return recorded
        time.sleep(0.02)
    pytest.fail("the node never recorded its address")


def _stop(address: node.NodeAddress) -> None:
    """Ask a node to stop the way a surface does — a request, never a signal."""
    request = urllib.request.Request(address.url_for("/api/shutdown"), method="POST")
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except OSError:
        pass  # already gone; the assertions below are what matter


def _get(address: node.NodeAddress, path: str) -> tuple[int, str]:
    """One request to a running node's route, as a client would make it."""
    try:
        with urllib.request.urlopen(address.url_for(path), timeout=15) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:  # a refusal is an answer, not an error
        return exc.code, exc.read().decode()


@pytest.fixture
def serve_node(tmp_path):
    """A real node in the ``serve`` posture, on an ephemeral port.

    Started in a thread (a node is a process the tests may not become) and
    stopped through its own shutdown route, exactly as the desktop build stops
    one. Yields the address the node recorded.
    """
    thread = threading.Thread(
        name="node-address-test",
        daemon=True,
        target=web.serve,
        kwargs={
            "host": "127.0.0.1",
            "port": 0,
            "open_browser": False,
            "data_dir": str(tmp_path / "data"),
        },
    )
    thread.start()
    address = _wait_for_record()
    try:
        assert node.ask() == address, "the recorded node must answer the one client"
        yield address
    finally:
        _stop(address)
        thread.join(_READY_TIMEOUT)
        assert not thread.is_alive(), "the node did not stop when asked"


# --- one declaration of the port ------------------------------------------- #


def test_the_node_port_has_a_single_declaration() -> None:
    """Every ``8765`` in the package is the one the node declares (integer literals only).

    A number restated per surface is a number that drifts: the console, the tray
    and both servers read ``DEFAULT_PORT``. Docstrings and comments may *name*
    the port (they explain the layout); what may not exist is a second place
    that decides it, so this walks the source's syntax and looks only at integer
    literals.
    """
    package = Path(paths.__file__).parent.parent
    declared: list[str] = []
    for source in sorted(package.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for parsed in ast.walk(tree):
            if isinstance(parsed, ast.Constant) and parsed.value == node.DEFAULT_PORT:
                declared.append(f"{source.relative_to(package)}:{parsed.lineno}")
    assert [hit.split(":", 1)[0] for hit in declared] == ["core/node.py"], declared


# --- a node records its address in every posture ---------------------------- #


def test_the_serve_posture_records_the_port_it_bound(serve_node) -> None:
    """``serve`` publishes the bound address where every surface resolves it.

    The node asked for port ``0``, so a recorded ``0`` (or anything but the port
    its socket holds) would be a guess. A clean stop clears the record again —
    the next node, not a stale file, is what a later surface finds.
    """
    recorded = serve_node
    assert recorded == node.recorded() == node.address()
    assert recorded.port != 0
    assert recorded.pid == os.getpid()
    # In the app-owned state directory (ADR-0025), as one file — the place every
    # surface resolves, not a temp file only the writer knows about.
    assert paths.node_address_path().parent == paths.resolve_state_dir()
    assert paths.node_address_path().is_file()

    _stop(recorded)
    deadline = time.monotonic() + _READY_TIMEOUT
    while node.recorded() is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert node.recorded() is None


def test_the_tray_posture_records_the_port_it_bound(tmp_path) -> None:
    """The tray starts a node too, and its node publishes the address the same way."""
    controller = ServiceController(port=0, data_dir=str(tmp_path / "data"))
    controller.start()
    try:
        recorded = _wait_for_record()
        assert recorded.port != 0
        assert recorded.host == "127.0.0.1"
        assert node.address() == recorded
    finally:
        assert controller.stop(timeout=_READY_TIMEOUT), "the node did not stop"
    assert node.recorded() is None, "a stopped node must not leave its address behind"


def test_the_tray_probes_the_address_the_record_names(tmp_path) -> None:
    """The tray's health probe dials what the node bound, not what it was asked for.

    Asked for port ``0``, the controller cannot know the port until the socket
    binds — so a probe built from its own request dials ``:0`` and reports a
    running node as unreachable. It reads the record instead, which is the
    address every other surface resolves.
    """
    controller = ServiceController(port=0, data_dir=str(tmp_path / "data"))
    controller.start()
    try:
        recorded = _wait_for_record()
        assert controller.address == recorded
        assert controller.url == recorded.url
        assert controller.healthy()
        assert controller.wait_until_ready(timeout=_READY_TIMEOUT)
    finally:
        assert controller.stop(timeout=_READY_TIMEOUT), "the node did not stop"


# --- a second process reads the address and reaches the node ---------------- #


def test_a_second_process_reads_the_address_and_reaches_the_node(serve_node) -> None:
    """The recorded address is enough for a *fresh* process to reach the node.

    Nothing is passed but the environment the recorded state lives in: the child
    resolves the same file, and its verb completes a request against the node —
    which is the whole point of recording an address instead of scanning.
    """
    child = subprocess.run(
        [sys.executable, "-c", _CHILD],
        env={
            **os.environ,
            "CR_STATE_DIR": str(paths.resolve_state_dir()),
            "CR_DATA_DIR": str(paths.resolve_data_dir()),
        },
        capture_output=True,
        text=True,
        timeout=_READY_TIMEOUT * 6,
    )
    assert child.returncode == 0, child.stderr
    assert serve_node.url in child.stdout, child.stdout


# --- nothing scans for a port ---------------------------------------------- #


def test_a_record_that_answers_nowhere_is_a_refusal_not_a_search(
    capsys, serve_node
) -> None:
    """A stale address fails as an absent one does — the live node is not hunted down.

    The node in ``serve_node`` is listening while this runs; the record is
    pointed at a port nothing holds. A surface that scanned would find the node
    anyway, and would therefore answer with an address no other surface has —
    and the console serving that very node refuses it too, because the record no
    longer names the socket it holds.
    """
    node.record(node.NodeAddress.of("127.0.0.1", _free_port()))

    with pytest.raises(node.NoNodeError) as excinfo:
        node.ask()
    assert str(excinfo.value) == node.NO_NODE_MESSAGE
    assert _command_line_answer(capsys)[0] == 1  # the command line refuses too
    status, body = _get(serve_node, "/api/node")
    assert (status, json.loads(body)["detail"]) == (503, node.NO_NODE_MESSAGE)


# --- the same answer from every surface ------------------------------------ #


def _console_answer(tmp_path) -> tuple[int, str]:
    """``GET /api/node`` on an app that is not a listening node."""
    app = create_app(
        Registry.open(data_dir=str(tmp_path / "console-data")),
        trusted_hosts=("testserver",),
    )
    with TestClient(app) as client:
        response = client.get("/api/node")
        return response.status_code, response.json()["detail"]


def _command_line_answer(capsys: pytest.CaptureFixture) -> tuple[int, str]:
    """Run the verb the way a shell does — through ``main``, whose exit code counts.

    ``CliRunner`` invokes a Click command directly, and Click's standalone mode
    discards the value a command *returns*; the console script's status is what a
    person's ``$?`` sees, so the test asks the same entry point the script does.
    """
    status = cli.main(["node"])
    captured = capsys.readouterr()
    return status, (captured.out + captured.err).strip()


@pytest.mark.parametrize("state", ("absent", "stale"))
def test_one_stated_answer_comes_from_every_surface(capsys, tmp_path, state) -> None:
    """Nothing listening is one sentence, on the command line, the console and MCP.

    ``absent`` is no record at all; ``stale`` is a record nothing answers. They
    are the same answer by construction — one error, one message — and this asks
    each surface for it rather than trusting that they share a call.
    """
    if state == "stale":
        node.record(node.NodeAddress.of("127.0.0.1", _free_port()))

    with pytest.raises(node.NoNodeError) as core:
        node.ask()
    console_status, console_detail = _console_answer(tmp_path)
    cli_status, cli_text = _command_line_answer(capsys)

    assert str(core.value) == node.NO_NODE_MESSAGE == node_line() == console_detail
    assert console_status == 503
    assert cli_status == 1
    assert cli_text == node.NO_NODE_MESSAGE


@pytest.mark.parametrize(
    "written",
    (
        '{"host": "127.0.0.1", "port": 8765, "pid": "not-a-number"}',
        '{"host": "127.0.0.1", "port": [8765], "pid": 1}',
        '{"host": ["127.0.0.1"], "port": 8765}',
        '{"host": "127.0.0.1", "port": "8765"}',
        "[]",
        "not json at all",
    ),
)
def test_a_record_that_cannot_be_read_is_no_address(capsys, tmp_path, written) -> None:
    """A field this build cannot read is no address — never an exception, ever.

    The record is a file on disk: it can be truncated, hand-edited, or written by
    something that is not this build. Reading it is therefore total — a bad
    ``pid`` used to raise out of ``recorded()``, which killed the command line
    with a traceback, answered the console with a 500 and stopped the MCP adapter
    starting — and a record that cannot be read is answered exactly as no record
    at all: the one sentence, from every surface.
    """
    paths.node_address_path().parent.mkdir(parents=True, exist_ok=True)
    paths.node_address_path().write_text(written, encoding="utf-8")

    assert node.recorded() is None
    with pytest.raises(node.NoNodeError) as core:
        node.ask()
    console_status, console_detail = _console_answer(tmp_path)
    cli_status, cli_text = _command_line_answer(capsys)

    assert str(core.value) == node.NO_NODE_MESSAGE == node_line() == console_detail
    assert (console_status, cli_status) == (503, 1)
    assert cli_text == node.NO_NODE_MESSAGE


# --- a node answers about itself, without a second request ------------------ #


def test_the_console_answers_concurrent_readers(serve_node) -> None:
    """``GET /api/node`` spends one request per reader, so readers cannot crowd it out.

    The route proved the node answered by requesting ``/api/health`` from itself,
    which took a second threadpool token per reader: a full pool turned into
    "nothing is listening" with the node answering right there. Every reader now
    gets the address the socket this app holds is bound to, and more readers than
    the pool holds must all get it.
    """
    answers: list[tuple[int, str]] = []
    lock = threading.Lock()

    def read() -> None:
        status, body = _get(serve_node, "/api/node")
        answer = (status, json.loads(body)["url"] if status == 200 else body)
        with lock:
            answers.append(answer)

    readers = [threading.Thread(target=read) for _ in range(48)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(_READY_TIMEOUT)

    assert sorted(answers) == [(200, serve_node.url)] * 48


def test_with_a_node_running_the_surfaces_name_the_same_address(
    capsys, serve_node
) -> None:
    """Where a node *is* recorded, every surface resolves that one address.

    The console answers with the record its own socket vouches for, the MCP
    adapter states it for the agent, and the command line prints it after
    completing a request — one address, three surfaces, no surface's own idea of
    a port.
    """
    status, body = _get(serve_node, "/api/node")
    assert status == 200, body
    assert json.loads(body)["url"] == serve_node.url
    assert json.loads(body)["port"] == serve_node.port
    assert node_line() == f"The clear-record node is listening at {serve_node.url}"

    cli_status, cli_text = _command_line_answer(capsys)
    assert cli_status == 0, cli_text
    assert serve_node.url in cli_text
