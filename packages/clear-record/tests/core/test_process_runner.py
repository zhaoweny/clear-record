"""Tests for the injectable process runner (no GPU, no whisper-cli).

``CancellableProcessRunner`` -- the seam in :mod:`clear_record.core.process`,
the one depth every layer can import -- is what lets the transcribe pool stop
its own in-flight children without monkey-patching ``subprocess.Popen``. These
tests spawn real Python children so the termination path is exercised: short
lived ones where only the ladder matters, and children that would sleep for 30
seconds but are stopped by the case under test.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from clear_record.core.process import (
    CancellableProcessRunner,
    ProcessCancelled,
    SubprocessRunner,
)


def test_subprocess_runner_captures_output() -> None:
    result = SubprocessRunner().run([sys.executable, "-c", "print('hi')"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hi"


def _start_run(runner: CancellableProcessRunner, cmd: list[str], *, count: int = 1):
    """Run ``runner.run`` on a thread and wait until ``count`` children register."""
    result: dict = {}

    def target() -> None:
        result["proc"] = runner.run(cmd, capture_output=True, text=True)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with runner._lock:
            if len(runner._procs) >= count:
                return thread, result
        time.sleep(0.01)
    raise AssertionError("child was never registered")


def test_cancelled_runner_refuses_new_launch() -> None:
    runner = CancellableProcessRunner()
    runner.cancel()
    with pytest.raises(ProcessCancelled):
        runner.run([sys.executable, "-c", "pass"])


def test_cancelling_one_runner_leaves_another_alone(tmp_path) -> None:
    """Scope is per runner: pool A's cancellation must not touch pool B."""
    a = CancellableProcessRunner()
    b = CancellableProcessRunner()
    marker = tmp_path / "b-finished.txt"
    code_b = (
        "import time, pathlib; time.sleep(0.4); "
        f"pathlib.Path({str(marker)!r}).write_text('ok')"
    )

    thread_a, result_a = _start_run(
        a, [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    thread_b, result_b = _start_run(b, [sys.executable, "-c", code_b])

    a.terminate_all(grace=1.0)
    thread_a.join(timeout=5)
    assert not thread_a.is_alive()
    assert result_a["proc"].returncode != 0  # A's child was terminated

    # B's child ran to completion, untouched by A's cancellation.
    thread_b.join(timeout=10)
    assert not thread_b.is_alive()
    assert result_b["proc"].returncode == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_timeout_kills_the_child_and_forgets_it() -> None:
    runner = CancellableProcessRunner()
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    with runner._lock:
        assert runner._procs == []


def test_the_pools_own_cancel_flag_does_not_cancel_a_running_child() -> None:
    """The runner's own flag is the pool's Ctrl-C, never the run's stop (RUN-04).

    The pool sets that flag in its interruption handler as it kills its children,
    so a wait that treated it as "report this chunk as cancelled" would rob the
    backend of the exit status it is entitled to see — the CLI's Ctrl-C reports
    itself as a killed child, and that is the behaviour this flag must preserve.
    """
    runner = CancellableProcessRunner()

    def cancel_soon() -> None:
        time.sleep(0.2)
        runner.cancel()  # the pool's own flag, as its interrupt handler sets it

    threading.Thread(target=cancel_soon, daemon=True).start()

    result = runner.run([sys.executable, "-c", "import time; time.sleep(0.4)"])

    assert result.returncode == 0, "the child exited on its own terms"
    assert runner.cancelled is True


def test_the_runs_cancel_signal_kills_a_child_that_is_already_running(
    tmp_path,
) -> None:
    """The run's own signal is what stops a decode in flight (RUN-04).

    A whole-file backend, or any tape inside one chunk, has a decode running for
    the length of the recording: without this the cancel would wait for it (and a
    decoder that never returns would hold the run, its meeting and the node).

    The child is the witness: it catches SIGTERM and writes a marker, so a runner
    that abandoned the process instead of killing it — which is exactly what a
    regression here looks like, and invisible to the runner's own bookkeeping —
    cannot pass this test.
    """
    marker = tmp_path / "child-terminated.txt"
    child = (
        "import pathlib, signal, time\n"
        "def on_term(*_):\n"
        f"    pathlib.Path({str(marker)!r}).write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "time.sleep(30)\n"
    )
    run_cancel = threading.Event()
    runner = CancellableProcessRunner(cancel=run_cancel)

    def cancel_soon() -> None:
        time.sleep(0.2)
        run_cancel.set()

    threading.Thread(target=cancel_soon, daemon=True).start()

    with pytest.raises(ProcessCancelled):
        runner.run([sys.executable, "-c", child])

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.exists(), "the child was killed, not abandoned"
    # And forgotten: a stale Popen here would be re-terminated by every later
    # ``terminate_all`` and re-scanned by every ``live_pids`` (the sampler's API),
    # and this branch is the only place a cancelled launch is dropped.
    with runner._lock:
        assert runner._procs == [], "the killed child was forgotten"


def test_a_cancelled_operation_kills_its_own_children_and_no_other(tmp_path) -> None:
    """A cancel ends the operation's children — and reaches nothing else.

    The bystander is a real, live process the operation never launched (the
    second pool's decoder, or the console's foreground ``tailscale serve``): it
    must run to completion while both of this runner's children are killed. This
    is the property the old global ``Popen`` patch could not promise, and the one
    a runner that signalled by anything but its own bookkeeping cannot pass.
    """
    marker = tmp_path / "bystander-finished.txt"
    bystander = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, time; time.sleep(0.8); "
            f"pathlib.Path({str(marker)!r}).write_text('ok')",
        ]
    )
    runner = CancellableProcessRunner()
    threads: list[threading.Thread] = []
    results: list[dict] = []
    for index in (1, 2):  # the second wait proves both are launched, not one
        thread, result = _start_run(
            runner,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            count=index,
        )
        threads.append(thread)
        results.append(result)

    runner.cancel()
    runner.terminate_all(grace=1.0)

    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "the operation's workers were released"
    for result in results:
        assert result["proc"].returncode != 0, "the operation's own child was killed"

    assert bystander.wait(timeout=10) == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_stop_ends_the_child_it_started_and_leaves_another_alone(tmp_path) -> None:
    """The long-lived form: ``start`` tracks one child, ``stop`` ends just that one.

    This is the console's foreground Serve shape — a child meant to outlive the
    call — so the handle has to be live and tracked until its owner stops it, and
    the ladder has to reach only the child that owner started.
    """
    marker = tmp_path / "bystander-finished.txt"
    runner = CancellableProcessRunner()
    child = runner.start(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        capture_output=True,
        text=True,
    )
    bystander = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, time; time.sleep(0.8); "
            f"pathlib.Path({str(marker)!r}).write_text('ok')",
        ]
    )
    assert child.poll() is None
    assert runner.live_pids() == (child.pid,)

    runner.stop(child, grace=1.0)

    assert child.poll() is not None, "the child it started is gone"
    assert runner.live_pids() == (), "and forgotten, so it is never signalled again"
    runner.stop(child, grace=1.0)  # idempotent: a stopped child is left alone

    assert bystander.wait(timeout=10) == 0
    assert marker.read_text(encoding="utf-8") == "ok"


def test_stop_escalates_to_kill_when_the_child_ignores_sigterm() -> None:
    """A child that outlives the grace is hard-killed, and the ladder is bounded."""
    runner = CancellableProcessRunner()
    child = runner.start(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)",
        ],
        capture_output=True,
        text=True,
    )
    began = time.monotonic()

    runner.stop(child, grace=0.3)

    assert child.poll() is not None, "SIGKILL followed the ignored SIGTERM"
    assert time.monotonic() - began < 5.0, "the grace bounds the ladder"


def test_run_keeps_cwd_env_and_check_meanings(tmp_path) -> None:
    """``cwd``/``env`` reach the child; ``check`` raises as ``subprocess.run`` does.

    ``engine.audio``'s ``ffmpeg`` decode relies on the ``check`` failure staying a
    ``CalledProcessError``; ``service.agent``'s command runner relies on the child
    running in the render directory with the caller's environment.
    """
    write = "import os, pathlib; pathlib.Path('marked.txt').write_text(os.environ['CR_SEAM_TEST'])"
    SubprocessRunner().run(
        [sys.executable, "-c", write],
        cwd=tmp_path,
        env={"CR_SEAM_TEST": "seam"},
    )
    assert (tmp_path / "marked.txt").read_text(encoding="utf-8") == "seam"

    with pytest.raises(subprocess.CalledProcessError) as plain:
        SubprocessRunner().run(
            [sys.executable, "-c", "raise SystemExit(3)"], check=True
        )
    assert plain.value.returncode == 3
    with pytest.raises(subprocess.CalledProcessError):
        CancellableProcessRunner().run(
            [sys.executable, "-c", "raise SystemExit(4)"], check=True
        )
