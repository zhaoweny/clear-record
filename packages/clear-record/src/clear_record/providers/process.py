"""Explicit, cancellable process runner for the system CLI backends.

The transcribe pool must stop in-flight ``whisper-cli`` children on Ctrl-C.
Rather than monkey-patching ``subprocess.Popen.__init__`` for the pool's
lifetime (which leaks into unrelated subprocesses and is unsafe when
``clear_record.cli`` is embedded or two pools run in one process), the pool
passes a
:class:`CancellableProcessRunner` *into* the backend, which launches every child
through it. Cancellation is therefore scoped to one runner -- and one pool.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Protocol

# How long a terminated child has to exit before it is hard-killed.
DEFAULT_CANCEL_GRACE_S = 1.0


class ProcessCancelled(RuntimeError):
    """Raised when a runner is asked to launch a child after cancellation."""


class ProcessRunner(Protocol):
    """Launch a child process and capture its output.

    A structural match for the subset of ``subprocess.run`` the backends use, so
    the default :class:`SubprocessRunner` is a drop-in and tests can inject a
    fake.
    """

    def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        """Run ``cmd`` to completion and return its result."""
        ...


class SubprocessRunner:
    """The default runner: a thin ``subprocess.run`` wrapper with no tracking."""

    def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=capture_output, text=text, timeout=timeout
        )


#: How long a launch waits before looking at the cancel signals again. Small
#: enough that a cancelled run stops promptly, long enough that the poll costs
#: nothing beside a decode that runs for seconds.
_CANCEL_POLL_S = 0.1


class CancellableProcessRunner:
    """Track this runner's children so a pool can terminate them on cancel.

    One instance per pool: :meth:`terminate_all` only touches children started
    through this instance, so a second concurrent pool is unaffected. Instances
    are safe to share across the pool's worker threads.

    ``cancel`` is the *run's* cancel signal (RUN-04), when this pool belongs to a
    run: an event another thread sets — the console asking a run to stop. It is
    read through :attr:`cancelled` beside this runner's own flag, so the pool's
    existing checks need no second condition: a requested cancel stops the pool
    exactly like Ctrl-C does.
    """

    def __init__(self, cancel: threading.Event | None = None) -> None:
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._cancel = cancel

    @property
    def cancelled(self) -> bool:
        """Whether a cancellation was requested: this runner's, or the run's."""
        return self._cancelled.is_set() or (
            self._cancel is not None and self._cancel.is_set()
        )

    def cancel(self) -> None:
        """Signal cancellation; later launches fail fast (see ``run``)."""
        self._cancelled.set()

    def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        if self.cancelled:
            raise ProcessCancelled("process runner cancelled before launch")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
        )
        with self._lock:
            self._procs.append(proc)
        try:
            out, err = self._wait(proc, cmd, timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self._forget(proc)
            raise
        except ProcessCancelled:
            # A cancel while this child was decoding (RUN-04): it is killed here,
            # where its handle is, rather than waiting for the decode to end — the
            # chunk is reported as cancelled, not as a decoder failure.
            self._terminate(proc)
            self._forget(proc)
            raise
        except BaseException:
            # Ctrl-C in the main thread can interrupt communicate(); make sure
            # the child dies before the exception propagates. In the pool path a
            # worker is parked in communicate(), so terminate_all() (not this
            # branch) does the killing.
            self._terminate(proc)
            self._forget(proc)
            raise
        self._forget(proc)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def _wait(
        self, proc: subprocess.Popen, cmd: list[str], timeout: float | None
    ) -> tuple[str | None, str | None]:
        """Wait for a child, honouring a cancel that arrives while it runs.

        ``communicate`` blocks until the child exits, so on its own it can only
        notice a cancellation *before* a launch or *after* the results are in.
        This waits in short slices instead and raises :class:`ProcessCancelled`
        as soon as a cancel is requested — which is what lets a run's stop reach a
        decode that is already in flight, and what kills a decoder that never
        returns. The caller's own ``timeout`` still means exactly what it meant:
        an expired deadline raises ``subprocess.TimeoutExpired``.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.cancelled:
                raise ProcessCancelled("process runner cancelled while decoding")
            slice_s = _CANCEL_POLL_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                slice_s = min(slice_s, remaining)
            try:
                return proc.communicate(timeout=slice_s)
            except subprocess.TimeoutExpired:
                # Not our deadline: look at the cancel signals and keep waiting.
                # Whatever the child has written is kept by the next call.
                continue

    def live_pids(self) -> tuple[int, ...]:
        """PIDs of the children this runner launched and is still waiting on.

        The transcribe pool samples its decoder workers' resident memory through
        this: a child is listed from its launch until its communicate() call
        returns, and only this runner launched it, so a caller samples exactly
        one pool's work and never an unrelated process. A child that has already
        exited is reaped here and left out, so a caller can never read a
        zombie's empty /proc entry and mistake it for zero memory.
        """
        with self._lock:
            procs = list(self._procs)
        return tuple(proc.pid for proc in procs if proc.poll() is None)

    def terminate_all(self, grace: float = DEFAULT_CANCEL_GRACE_S) -> None:
        """Ask tracked children to stop, then hard-kill any survivors.

        Terminating closes the child's pipes, which unblocks a worker parked in
        ``communicate`` promptly; the bounded grace lets it exit cleanly before
        ``kill`` is used as a fallback.
        """
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            self._terminate(proc)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if all(proc.poll() is not None for proc in procs):
                return
            time.sleep(0.02)
        for proc in procs:
            self._kill(proc)

    def _forget(self, proc: subprocess.Popen) -> None:
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
        except Exception:
            pass

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except Exception:
            pass


__all__ = [
    "DEFAULT_CANCEL_GRACE_S",
    "CancellableProcessRunner",
    "ProcessCancelled",
    "ProcessRunner",
    "SubprocessRunner",
]
