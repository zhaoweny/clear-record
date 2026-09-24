"""The invocation a person types ensures a node: attach, or bring one up.

The node is a process of its own (``clear-record serve``), so these tests start
real ones. Everything a node resolves — its recorded address, its registry, its
diagnostics sink — goes through ``CR_*``, so a test's node is invisible to the
machine's own; only its port has to be chosen here, because the node's default
bind is one declaration (:mod:`clear_record.core.node`) and one machine-wide port
is not a thing a test may hold. Each test stops the node it started, by pid.
"""

from __future__ import annotations

import os
import signal
import socket
import time

import pytest

from clear_record.cli import cli
from clear_record.core import node


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_exit(pid: int, timeout: float = 10.0) -> None:
    """Wait until the node is gone — reaped, not merely a zombie of ours."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:  # not this process's child after all
            reaped = 0
        if reaped == pid:
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"node {pid} did not stop")


@pytest.fixture
def node_port(tmp_path, monkeypatch) -> int:
    """A machine with this command's own state dirs and a node port of its own.

    The port is patched onto the argv the command starts a node with; the default
    bind itself is still the node's own declaration, and nothing here restates it.
    On the way out, a node the test left running is stopped, so no node outlives
    the test that started it.
    """
    port = _free_port()
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    real_argv = cli._node_argv
    monkeypatch.setattr(cli, "_node_argv", lambda: real_argv() + ["--port", str(port)])
    monkeypatch.setattr(cli, "_NODE_RACE_GRACE", 0.2)
    yield port
    address = node.recorded()
    if address is not None and address.pid != os.getpid():
        try:
            os.kill(address.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            _wait_for_exit(address.pid)


def _recorded_work(monkeypatch) -> list[str]:
    """Record what `run` was asked to do, in place of the pipeline itself."""
    calls: list[str] = []

    def fake(directory: str, options) -> None:
        calls.append(directory)

    monkeypatch.setattr(cli, "_run_pipeline", fake)
    return calls


def _workspace(tmp_path):
    directory = tmp_path / "meeting"
    directory.mkdir()
    return directory


def test_from_a_cold_start_the_command_leaves_a_node_running_and_the_work_done(
    node_port, monkeypatch, tmp_path
) -> None:
    """One command: a node is up afterwards, and the work it was asked for is done."""
    work = _recorded_work(monkeypatch)
    directory = _workspace(tmp_path)

    assert node.recorded() is None, "the test starts from a cold machine"
    assert cli.main(["run", str(directory)]) == 0

    assert work == [str(directory)], "the work the command was asked for is done"
    address = node.recorded()
    assert address is not None, "the command left a node behind"
    assert address.pid != os.getpid(), "the node is a process of its own"
    assert node.ask() == address, "and it answers as every surface reaches it"


def test_with_a_node_already_up_the_command_attaches_instead_of_starting_a_second(
    node_port, monkeypatch, tmp_path
) -> None:
    """The same command twice starts a node once; the second attaches."""
    work = _recorded_work(monkeypatch)
    directory = _workspace(tmp_path)
    started: list[int] = []
    real_start = cli._start_node
    monkeypatch.setattr(cli, "_start_node", lambda: started.append(1) or real_start())

    assert cli.main(["run", str(directory)]) == 0
    first = node.ask()
    assert cli.main(["run", str(directory)]) == 0

    assert started == [1], "the second command started no node"
    assert work == [str(directory)] * 2, "and both commands did their work"
    assert node.ask() == first, "the node it attached to is the one already up"


def test_a_machine_where_no_node_can_start_states_one_sentence_and_runs_no_work(
    node_port, monkeypatch, tmp_path, capsys
) -> None:
    """The node is the tool: no node means one sentence, never an in-process run."""
    work = _recorded_work(monkeypatch)
    directory = _workspace(tmp_path)
    # A listener that is not a node holds the port the node would bind, so no node
    # can start here — the machine the direction's one honest sentence is for.
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", node_port))
        busy.listen(1)
        with pytest.raises(SystemExit) as exit:
            cli.main(["run", str(directory)])

    assert exit.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == cli._NODE_IS_THE_TOOL
    assert "\n" not in captured.err.strip(), "one sentence, not a paragraph"
    assert work == [], "there is no second implementation to fall back into"
