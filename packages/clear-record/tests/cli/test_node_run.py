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
so a whole run is exercised with no model in the picture, over a real tape — and,
where a test gates the queue, the pipeline itself.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli import cli
from clear_record.core import (
    DECODER_KNOB_FIELDS,
    JobEvent,
    Progress,
    Segment,
    TranscriptionResult,
)
from clear_record.core import node
from clear_record.pipeline import stages
from clear_record.pipeline.auto import MODEL_LADDER
from clear_record.providers import BackendBase, BackendInfo
from clear_record.service.lifecycle import QUEUED, RUN_ORIGINS
from clear_record.web.app import MODEL_IS_THE_NODES

#: How long a run gets to finish once nothing is gating it.
_RUN_TIMEOUT = 30.0


class _FakeBackend(BackendBase):
    """A deterministic decoder: one segment spanning each chunk it is handed.

    It advertises every decoder knob (``DECODER_KNOB_FIELDS``) because it takes
    them all through ``**decoder_knobs`` and ignores them: a test whose run
    resolves a **profile** — the one that fills those knobs — then still
    executes, instead of failing a run on a capability the fake really has.
    """

    info = BackendInfo(
        id="fake",
        vendor="test",
        frameworks=(),
        description="fake decoder",
        default_model="fake-model",
        decoder_knobs=DECODER_KNOB_FIELDS,
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


def _in_thread(target) -> tuple[threading.Thread, list[Any]]:
    """Run *target* on its own thread, collecting how it ended.

    A command that ends by **raising** is collected as the exception it raised:
    ``cli.main`` returns a code for the errors Click itself reports, but a failure
    the command surface composes leaves as a ``SystemExit`` — the same end the
    process boundary has, sentence and all — and a test wants to read both the
    code and the sentence.
    """
    out: list[Any] = []

    def body() -> None:
        try:
            out.append(target())
        except BaseException as exc:  # the command's own exit is a result here
            out.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, out


def _models_on_disk(root: Path) -> Path:
    """A models directory holding every ladder checkpoint (empty files do).

    ``--auto`` never downloads, so a machine with nothing on disk is **refused**
    rather than explained; a test that wants the explanation gives its probe a
    directory where whichever model the resolver picks is already present. The
    resolver only looks at presence, and the decoder is faked, so the files are
    never read.
    """
    models = root / "models"
    models.mkdir()
    for size in MODEL_LADDER:
        (models / f"ggml-{size}.bin").touch()
    return models


def _unreachable(address: node.NodeAddress) -> bool:
    """Whether nothing answers at *address* any more."""
    try:
        node.reach(address, timeout=0.2)
    except node.NoNodeError:
        return True
    return False


def _wait_for(predicate, *, timeout: float = _RUN_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_for_line(capsys, text: str, *, timeout: float = _RUN_TIMEOUT) -> bool:
    """Whether *text* reaches stdout within *timeout* (consuming what it reads).

    For output a command prints while a test still holds the node's queue: the
    read is destructive (``readouterr``) because the point is to see the line as
    it appears, not to collect everything one command printed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in capsys.readouterr().out:
            return True
        time.sleep(0.05)
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
        # The stage's words, as the pipeline reports them: a line event whose
        # ``message`` is the text, beside the progress reports (which carry
        # counters and no words at all).
        progress = Progress("transcribe", 2, on_event)
        progress.start()
        on_event(
            JobEvent(
                stage="transcribe",
                index=1,
                total=2,
                source="a",
                message="[transcribe]   a chunk 1/2 -> 1 segment(s)",
            )
        )
        progress.advance(source="a")
        gate.wait(_RUN_TIMEOUT)
        on_event(
            JobEvent(
                stage="transcribe",
                index=2,
                total=2,
                source="b",
                message="[transcribe]   b chunk 2/2 -> 1 segment(s)",
            )
        )
        progress.advance(source="b")
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


def test_the_node_owns_a_command_line_run_while_it_runs_and_after(
    gated_node, tmp_path, capsys
) -> None:
    """The console shows the run while it runs, and after it finishes.

    The proof is the console's own run list over the *same* registry the node
    wrote: the run is there — with the command line as its origin — while the
    pipeline is still held, and it is there as ``done`` once the run ends. The
    command line's own output is the run's node-side stream: every word the
    stage reported, once each, and where the run ended.
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
    # The run's own words, read back off the run's stream: one line per chunk the
    # fake reported, each printed **once**, followed by the surface's own end.
    # A progress report carries counters and no words, so those events add no
    # line — and a follower that printed every event, or a line twice, fails the
    # exact sequence below.
    lines = printed.splitlines()
    assert lines[:3] == [
        "[transcribe]   a chunk 1/2 -> 1 segment(s)",
        "[transcribe]   b chunk 2/2 -> 1 segment(s)",
        "[run] #1 done",
    ], "the run's own stream"
    assert lines[3].startswith("[next] the record is in ")


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
    gated_node, tmp_path, capsys
) -> None:
    """A second command-line run is ``queued`` while the node is busy.

    Both runs are the command line's, over two directories, so both are node runs
    in the one queue. The held run is the one executing; the second is at the
    back of the FIFO, executes only after the first ends, and the pipeline is
    entered once at a time — the client never executes anything itself. And it
    **says** it is waiting: the follower prints the queue place the node reports
    (``cli.runs.follow``), so a queued run does not read as a hung one.
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
        # The waiting client says where it is, and it takes a **poll** to say it
        # (a queue place is printed from a read the loop makes after its first),
        # which is why this waits for the line and not only for the row. The gate
        # is still held, so nothing here started the run it waits behind.
        assert _wait_for_line(capsys, "[queued] position 1"), (
            "the waiting run said nothing about its queue place"
        )
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
    assert "[run] #2 done" in printed, "the run's own id and its end"
    assert "[next] the record is in" in printed
    assert f"{workspace.resolve()}/export" in printed


def test_a_relative_glossary_is_the_clients_file_not_the_nodes_cwd(
    node_in_this_process, tmp_path, monkeypatch
) -> None:
    """``--glossary`` is resolved where the client stands, exactly like ``<directory>``.

    The node resolves a relative path against **its own** cwd, so a relative flag
    names a different file depending on who reads it. The command line resolves
    the directory it sends; the glossary has to travel the same way, or the node
    decodes with another directory's file (or none) and still reports done. The
    two parties are made to stand in different directories inside one run: the
    client posts from one, the node's pipeline reads in the other, and what it
    opens is the client's file.
    """
    client_dir = tmp_path / "client"
    node_dir = tmp_path / "node"
    client_dir.mkdir()
    node_dir.mkdir()
    (client_dir / "terms.txt").write_text("CLIENTTERM\n", encoding="utf-8")
    (node_dir / "terms.txt").write_text("DECOYTERM\n", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    read: list[str] = []

    def pipeline(directory, options, on_event) -> None:
        # The node's own read of the glossary it was handed — what transcribe's
        # ``_load_glossary`` opens — against the cwd the node runs in.
        entered.set()
        assert release.wait(_RUN_TIMEOUT)
        read.append(Path(options.glossary).read_text(encoding="utf-8"))

    workspace = _workspace(tmp_path, "glossary-cwd", tapes=1)
    monkeypatch.chdir(client_dir)
    with node_in_this_process(pipeline=pipeline):
        thread, out = _in_thread(lambda: _cli_run(workspace, "--glossary", "terms.txt"))
        assert entered.wait(_RUN_TIMEOUT), "the run never reached the node"
        # The client has posted; the node runs where *it* stands.
        monkeypatch.chdir(node_dir)
        release.set()
        thread.join(_RUN_TIMEOUT)

    assert not thread.is_alive()
    assert out == [0], out
    assert read == ["CLIENTTERM\n"], "the node read another directory's glossary"


# --- what `--auto` chose, in the node's own words -------------------------- #
def test_an_auto_run_prints_the_explanation_the_node_recorded(
    node_in_this_process, tmp_path, monkeypatch, capsys
) -> None:
    """``--auto`` keeps its promise — "explain the choice" — from the node's row.

    The resolver runs on the **node** (the run is the node's), so the account of
    what it chose is the node's too: it is recorded in the run's meta
    (``RunOut.options``), which is what the console's run row renders and what
    this pins. The expected sentence is read back out of the node's own registry,
    so this cannot pass on an explanation the command line invented.
    """
    monkeypatch.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
    monkeypatch.setenv("CR_MODELS_DIR", str(_models_on_disk(tmp_path)))
    workspace = _workspace(tmp_path, "auto", tapes=1)
    with node_in_this_process() as node_here:
        assert _cli_run(workspace, "--auto") == 0
        run = node_here.registry.list_runs(_meeting_id(node_here, workspace))[0]
    recorded = (run.options or {}).get("auto") or {}
    assert recorded.get("explanation"), "the node recorded no auto explanation"
    printed = capsys.readouterr().out
    assert recorded["explanation"] in printed, "the choice was not explained"
    assert printed.index(recorded["explanation"]) < printed.index("[run] #"), (
        "the explanation came after the run it explains"
    )


# --- the node can go away under a follower --------------------------------- #
def test_a_node_that_goes_away_mid_follow_is_one_sentence(gated_node, tmp_path) -> None:
    """A node lost mid-follow reads as the one sentence, not as a traceback.

    The command line is a client (ADR-0032), so the node can go away at any step
    of a run — here while the run it accepted is being followed, after the
    submission was answered. What a person gets is the sentence ``core.node``
    wrote for exactly that (``node.NO_NODE_MESSAGE``) and a non-zero end: an
    uncaught :class:`~clear_record.core.node.NoNodeError` would print the
    traceback a bug in the client looks like.
    """
    workspace = _workspace(tmp_path, "vanished", tapes=1)
    thread, ended = _in_thread(lambda: _cli_run(workspace))
    try:
        assert _wait_for(lambda: gated_node.entered), "the node never started the run"
        gated_node.server.should_exit = True
        assert _wait_for(lambda: _unreachable(gated_node.address)), (
            "the node never went away"
        )
    finally:
        gated_node.gate.set()
    thread.join(_RUN_TIMEOUT)
    assert not thread.is_alive(), "the command line never returned"
    assert len(ended) == 1, "the thread ended with neither a code nor an exception"
    failure = ended[0]
    assert isinstance(failure, SystemExit), f"{failure!r} was not the command's end"
    assert str(failure) == str(node.NoNodeError()), "not the node's own sentence"


# --- what a client may set -------------------------------------------------- #
def test_the_command_line_passes_its_knobs_through_unchanged(
    node_in_this_process, tmp_path, monkeypatch
) -> None:
    """Every knob ``run`` accepts reaches the node's row with the value it was given.

    A flag is a field of the node's run request (``RunCreate``), so one run walks
    the whole knob surface — the chunking pair and the worker count, the seven
    decoder knobs, the glossary file and the re-run scope — and every value is
    read back off the run's own row (``run_options``): what the user typed is what
    the node received, with nothing translated on the way, nothing invented, and
    no default filled in behind them. The glossary is a path like the directory,
    so it is the same file on this machine; the node records its hash beside it.
    """
    monkeypatch.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
    workspace = _workspace(tmp_path, "knobs", tapes=1)
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("ZX-2000\n", encoding="utf-8")
    flags = (
        "--chunk-seconds",
        "30",
        "--overlap-seconds",
        "1",
        "--jobs",
        "1",
        "--beam-size",
        "4",
        "--best-of",
        "2",
        "--temperature",
        "0.2",
        "--entropy-thold",
        "2.4",
        "--no-speech-thold",
        "0.6",
        "--max-context",
        "-1",
        "--threads",
        "2",
        "--glossary",
        str(glossary),
        "--rerun-source",
        "a",
        "--rerun-range",
        "0:00-0:06",
    )
    with node_in_this_process() as node_here:
        assert _cli_run(workspace, *flags) == 0
        run = node_here.registry.list_runs(_meeting_id(node_here, workspace))[0]

    expected = {
        "chunk_seconds": 30.0,
        "overlap_seconds": 1.0,
        "jobs": 1,
        "beam_size": 4,
        "best_of": 2,
        "temperature": 0.2,
        "entropy_thold": 2.4,
        "no_speech_thold": 0.6,
        "max_context": -1,
        "threads": 2,
        "glossary": str(glossary),
        "rerun_sources": ("a",),
        "rerun_range": "0:00-0:06",
    }
    recorded = run.run_options
    assert {name: recorded[name] for name in expected} == expected
    assert (run.options or {}).get("glossary_sha256"), "the glossary was not identified"


def test_the_request_run_sends_is_exactly_the_run_api_it_speaks_to() -> None:
    """The body ``run`` sends carries every field the node declares, and no other.

    The command surface may not import ``web`` (ADR-0004/ADR-0012), so nothing but
    a test ties the client to the node's own declaration: a knob the run API gains
    and this client forgets would be refused at a user's run (the API refuses a
    field it does not declare) or silently defaulted, and a field this client
    invents would be refused before it ever reached a client. ``directory`` is the
    workspace route's own subject, added by :func:`clear_record.cli.runs.start`.
    """
    from clear_record.web.app import WorkspaceRunCreate

    command = cli._build_group().commands["run"]
    with command.make_context("run", ["dir"]) as ctx:
        args = SimpleNamespace(**ctx.params)
    body = cli._node_run_body(args, split="auto")

    assert set(body) == set(WorkspaceRunCreate.model_fields) - {"directory"}


# --- what a client may not set --------------------------------------------- #
def test_a_flag_a_node_run_cannot_carry_is_refused(
    node_in_this_process, tmp_path, capsys
) -> None:
    """A flag the node's run API does not declare is refused, never dropped.

    Nothing is written: the refusal happens before anything is asked of the node,
    and the sentence names every flag the user set, so one edit fixes them all.
    What is refused is what the request has no field for — a stage or probe flag
    (``--diarize``, ``--speakers``) or the models directory, which is the node's
    own; the run **knobs** are all carried now, so none of them is refused.
    """
    workspace = _workspace(tmp_path, "refused", tapes=1)
    with node_in_this_process() as node_here:
        assert _cli_run(workspace, "--diarize", "--speakers", "2") != 0
        captured = capsys.readouterr()
        assert "--diarize" in captured.err and "--speakers" in captured.err
        assert "a node run cannot set" in captured.err
        assert node_here.registry.list_meetings() == []


def test_a_normalized_models_dir_is_not_a_flag_the_user_typed(
    node_in_this_process, tmp_path, monkeypatch
) -> None:
    """``CR_MODELS_DIR`` in a normalized form is still *unset*, not a typed flag.

    ``--models-dir`` is the node's own, so a node run refuses it — but only when
    the user **asked**. The comparison is between directories, not their spelling:
    a value like ``…/models/`` names the directory the resolver names, so ``run``
    proceeds rather than refusing a flag nobody typed.
    """
    monkeypatch.setattr(stages, "get_backend", lambda _backend_id: _FakeBackend())
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path / "models") + os.sep)
    workspace = _workspace(tmp_path, "normalized-models", tapes=1)
    with node_in_this_process():
        assert _cli_run(workspace) == 0


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


def test_a_model_named_as_a_path_is_refused_in_the_nodes_own_words(
    node_in_this_process, tmp_path
) -> None:
    """A model must be on the node: the node refuses a path, and this prints it.

    ``--model`` *is* a flag a node run carries, so the refusal cannot be the
    command line's own — and it must not be a second wording of the rule either:
    the node owns the sentence, and a person reads exactly that sentence here
    (``web.app.MODEL_IS_THE_NODES``), not a traceback or a raw JSON detail.
    """
    workspace = _workspace(tmp_path, "path-model", tapes=1)
    with node_in_this_process() as node_here:
        with pytest.raises(SystemExit) as ended:
            _cli_run(workspace, "--model", str(tmp_path / "ggml-mine.bin"))
        # Refused before anything was asked of the registry.
        assert node_here.registry.list_meetings() == []
    assert str(ended.value) == MODEL_IS_THE_NODES
