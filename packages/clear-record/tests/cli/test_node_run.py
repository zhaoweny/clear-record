"""A run the command line starts is a run the node owns (ADR-0032).

The relation these tests prove is not "``run`` speaks HTTP". It is:

- a run started by ``clear-record run`` is **the node's run**: it is in the
  registry while it runs and after it finishes, and the console's run list shows
  it with the command line as its **origin** (``cli`` — a value the service
  already declares for the surface);
- it **joins the one run per node queue**: while another run holds the node the
  command line's run waits, ``queued``, and the node's own claim — never the
  client — is what executes it;
- a run over a **local directory** works when client and node are the same
  machine: the node registers the directory as a meeting, the directory's audio
  is that meeting's tape set, and the pipeline's artifacts land in that
  directory;
- a flag the node's run API does not carry is **refused**, not quietly dropped.

The node comes from ``conftest.py`` (a real app on an ephemeral port, its address
recorded) and the client is the real command line; only the ASR backend is faked,
so a whole run is exercised with no model and no tape.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli import cli
from clear_record.core import Progress, Segment, TranscriptionResult
from clear_record.pipeline import stages
from clear_record.providers import BackendBase, BackendInfo
from clear_record.service.lifecycle import QUEUED, RUN_ORIGINS

#: How long a run gets to finish once nothing is gating it.
_RUN_TIMEOUT = 30.0


class _FakeBackend(BackendBase):
    """A deterministic decoder: one segment spanning each chunk it is handed."""

    info = BackendInfo(
        id="fake",
        vendor="test",
        frameworks=(),
        description="fake decoder",
        default_model="fake-model",
    )

    def available(self) -> bool:
        return True

    def prepare(self, model: str | None, model_dir: str | None) -> str:
        return "fake-model.bin"

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
        process_runner=None,
        **decoder_knobs: object,
    ) -> TranscriptionResult:
        data, sample_rate = sf.read(audio_path)
        duration = len(data) / sample_rate
        return TranscriptionResult(
            source="fake",
            segments=(Segment(0.0, round(duration, 3), "chunk", "fake"),),
            language="en",
            backend="fake",
            model="fake-model",
            audio_duration=duration,
        )


def _chirp(path: Path) -> None:
    """Six seconds of aperiodic tone — audio the pipeline can actually align."""
    sample_rate = 8000
    seconds = np.arange(int(6.0 * sample_rate), dtype=np.float64) / sample_rate
    sweep = (400.0 - 100.0) / (2.0 * 6.0)
    phase = 2 * np.pi * (100.0 * seconds + sweep * seconds * seconds)
    sf.write(str(path), (0.4 * np.sin(phase)).astype(np.float32), sample_rate)


def _workspace(root: Path, name: str, *, tapes: int = 2) -> Path:
    workspace = root / name
    workspace.mkdir()
    for index in range(tapes):
        _chirp(workspace / f"{chr(ord('a') + index)}.wav")
    return workspace


def _cli_run(directory: Path, *flags: str) -> int:
    """Run the real command line, through the real parser."""
    return cli.main(["run", str(directory), *flags])


def _in_thread(target) -> tuple[threading.Thread, list[int]]:
    """Run *target* on its own thread, collecting its return value."""
    out: list[int] = []

    def body() -> None:
        out.append(target())

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, out


def _wait_for(predicate, *, timeout: float = _RUN_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _meeting_id(node_here, workspace: Path) -> int:
    """The meeting the node registered for *workspace* (its one translation)."""
    meeting = node_here.registry.meeting_for_workspace(str(workspace))
    assert meeting is not None
    return meeting.id


@pytest.fixture()
def gated_node(node_in_this_process, tmp_path):
    """A node whose pipeline waits on a gate, and records that it was entered."""
    gate = threading.Event()
    entered: list[int] = []

    def pipeline(directory: str, options, on_event) -> None:
        entered.append(1)
        progress = Progress("transcribe", 2, on_event)
        progress.start("transcribing")
        progress.advance(source="a", message="chunk 1")
        gate.wait(_RUN_TIMEOUT)
        progress.advance(source="b", message="chunk 2")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")

    with node_in_this_process(pipeline=pipeline) as node_here:
        node_here.gate = gate
        node_here.entered = entered
        yield node_here


# --- the run is the node's ------------------------------------------------- #
def test_a_command_line_run_is_a_row_the_node_owns(
    node_in_this_process, tmp_path, monkeypatch
) -> None:
    """The smallest statement of the relation: the **node** has the run.

    Before ADR-0032's facade, ``clear-record run`` ran the pipeline in its own
    process and wrote no run row at all — the run succeeded and the node's
    registry stayed empty. So this is the relation the ticket lands, in one
    assertion: the workspace the command line named is a meeting this node
    registered, because the run that named it is the node's.
    """
    monkeypatch.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
    workspace = _workspace(tmp_path, "owned", tapes=1)
    with node_in_this_process() as node_here:
        assert _cli_run(workspace) == 0
        meetings = node_here.registry.list_meetings()
    assert [meeting.workspace_path for meeting in meetings] == [
        str(workspace.resolve())
    ]


def test_a_command_line_run_is_the_node_s_while_it_runs_and_after(
    gated_node, tmp_path, capsys
) -> None:
    """The console shows the run while it runs, and after it finishes.

    The proof is the console's own run list over the *same* registry the node
    wrote: the run is there — with the command line as its origin — while the
    pipeline is still held, and it is there as ``done`` once the run ends. The
    command line's own output is the run's node-side stream: the stage the run
    closed, and where the run ended.
    """
    workspace = _workspace(tmp_path, "held")
    thread, codes = _in_thread(lambda: _cli_run(workspace))
    try:
        assert _wait_for(lambda: gated_node.entered), "the node never started the run"
        run = gated_node.registry.list_runs(_meeting_id(gated_node, workspace))[0]
        live = gated_node.console.get("/activity").text
        while_running = {
            "status": run.status,
            "origin": run.origin,
            "console row": f'<div class="run status-{run.status}">' in live,
            "console origin": "<dd>cli</dd>" in live,
        }
    finally:
        gated_node.gate.set()
    thread.join(_RUN_TIMEOUT)
    assert not thread.is_alive(), "the command line never returned"
    assert codes == [0], "a finished run is the command line's success"
    assert while_running == {
        "status": "running",
        "origin": "cli",
        "console row": True,
        "console origin": True,
    }
    after = gated_node.registry.list_runs(_meeting_id(gated_node, workspace))[0]
    history = gated_node.console.get("/activity").text
    assert after.status == "done"
    assert '<div class="run status-done">' in history
    assert "cli" in history
    printed = capsys.readouterr().out
    assert "[transcribe] 2 / 2" in printed, "the run's own stream"
    assert "[run] 1 done" in printed


def test_a_command_line_run_carries_the_command_line_as_its_origin(
    gated_node, tmp_path
) -> None:
    """The recorded origin is ``cli``, a value the service already declares.

    The client's constant and the service's vocabulary are tied together here:
    the command surface may not import ``service`` (ADR-0004), so this test is
    what keeps the wire value from drifting away from the declaration the node
    validates against.
    """
    workspace = _workspace(tmp_path, "origin")
    thread, codes = _in_thread(lambda: _cli_run(workspace))
    try:
        assert _wait_for(lambda: gated_node.entered)
        run = gated_node.registry.list_runs(_meeting_id(gated_node, workspace))[0]
    finally:
        gated_node.gate.set()
    thread.join(_RUN_TIMEOUT)
    assert codes == [0]
    assert cli.CLI_ORIGIN == "cli"
    assert cli.CLI_ORIGIN in RUN_ORIGINS
    assert run.origin == cli.CLI_ORIGIN


# --- one queue, and the node decides who runs ------------------------------ #
def test_a_command_line_run_waits_while_another_run_holds_the_node(
    gated_node, tmp_path
) -> None:
    """A second command-line run is ``queued`` while the node is busy.

    Both runs are the command line's, over two directories, so both are node runs
    in the one queue. The held run is the one executing; the second is at the
    back of the FIFO, executes only after the first ends, and the pipeline is
    entered once at a time — the client never executes anything itself.
    """
    first = _workspace(tmp_path, "first")
    second = _workspace(tmp_path, "second")
    first_thread, first_codes = _in_thread(lambda: _cli_run(first))
    assert _wait_for(lambda: gated_node.entered), "the first run never started"
    second_thread, second_codes = _in_thread(lambda: _cli_run(second))

    def second_run():
        runs = gated_node.registry.list_runs(_meeting_id(gated_node, second))
        return runs[0] if runs else None

    try:
        assert _wait_for(lambda: second_run() is not None), "never submitted"
        assert _wait_for(lambda: second_run().status == QUEUED), "never queued"
        assert len(gated_node.entered) == 1, "two runs executed at once"
    finally:
        gated_node.gate.set()
    first_thread.join(_RUN_TIMEOUT)
    second_thread.join(_RUN_TIMEOUT)
    assert first_codes == [0] and second_codes == [0]
    assert len(gated_node.entered) == 2, "the queued run never executed"


# --- a local directory, client and node on one machine --------------------- #
def test_a_run_over_a_local_directory_works_when_client_and_node_agree(
    node_in_this_process, tmp_path, monkeypatch, capsys
) -> None:
    """``clear-record run <dir>`` runs *that* directory on the node.

    The whole path is exercised for real: the node's queue executes the pipeline
    (with the decoder faked, as the suite always does), the artifacts land in the
    directory the client named, the run is the node's row, and a second run over
    the same directory is the *same* meeting — one row per run, never a second
    meeting per invocation.
    """
    monkeypatch.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
    workspace = _workspace(tmp_path, "local")
    with node_in_this_process() as node_here:
        assert _cli_run(workspace) == 0
        assert _cli_run(workspace) == 0
        meeting = node_here.registry.meeting_for_workspace(str(workspace))
        assert meeting is not None
        assert meeting.workspace_path == str(workspace.resolve())
        runs = node_here.registry.list_runs(meeting.id)
        tape_set = node_here.registry.latest_recording_set(meeting.id)
        assert len(node_here.registry.list_meetings()) == 1, (
            "one workspace, one meeting"
        )
    assert [run.status for run in runs] == ["done", "done"], "two runs, newest first"
    assert {run.origin for run in runs} == {"cli"}
    assert tape_set is not None
    assert {Path(path).name for path in tape_set.paths} == {"a.wav", "b.wav"}
    assert (workspace / "export" / "record.md").is_file()
    assert (workspace / "manifest.json").is_file()
    printed = capsys.readouterr().out
    assert "[run] 2 done" in printed, "the run's own id and its end"
    assert "[next] the record is in" in printed
    assert f"{workspace.resolve()}/export" in printed


# --- what a client may not set --------------------------------------------- #
def test_a_flag_a_node_run_cannot_carry_is_refused(
    node_in_this_process, tmp_path, capsys
) -> None:
    """A knob the node's run API does not declare is refused, never dropped.

    Nothing is written: the refusal happens before anything is asked of the node,
    and the sentence names every flag the user set, so one edit fixes them all.
    """
    workspace = _workspace(tmp_path, "refused", tapes=1)
    with node_in_this_process() as node_here:
        assert _cli_run(workspace, "--diarize", "--chunk-seconds", "60") != 0
        captured = capsys.readouterr()
        assert "--diarize" in captured.err and "--chunk-seconds" in captured.err
        assert "a node run cannot set" in captured.err
        assert node_here.registry.list_meetings() == []


def test_every_flag_run_exposes_is_carried_or_refused() -> None:
    """A new flag on ``run`` cannot ride along and be silently dropped.

    The carried set and the refusal list are declarations; this ties them to the
    parser ``run`` is actually built from — every option the command exposes is in
    one of the two, so adding a flag without deciding what a node run does with it
    fails here, named.
    """
    command = cli._build_group().commands["run"]
    exposed = {param.name for param in command.params} - {"directory", "verbose"}
    declared = {name for name, _ in cli._NODE_RUN_UNSETTABLE} | set(
        cli._NODE_RUN_CARRIED
    )
    assert exposed == declared
