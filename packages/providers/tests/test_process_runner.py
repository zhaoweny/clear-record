"""Tests for the injectable process runner (no GPU, no whisper-cli).

``CancellableProcessRunner`` is what lets the transcribe pool stop its own
in-flight children without monkey-patching ``subprocess.Popen``. These tests
spawn real, short-lived Python children so the termination path is exercised.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from cr_providers.process import (
    CancellableProcessRunner,
    ProcessCancelled,
    SubprocessRunner,
)


def test_subprocess_runner_captures_output() -> None:
    result = SubprocessRunner().run([sys.executable, "-c", "print('hi')"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hi"


def _start_run(runner: CancellableProcessRunner, cmd: list[str]):
    """Run ``runner.run`` on a thread and wait until its child is registered."""
    result: dict = {}

    def target() -> None:
        result["proc"] = runner.run(cmd, capture_output=True, text=True)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with runner._lock:
            if runner._procs:
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
