"""The one process seam: launch a child, capture it, stop exactly your own.

Every layer that runs a child process launches it through this module — the
transcribe pool's ``whisper-cli`` children, the console's foreground
``tailscale serve``, the system TTS engines, the Apple speech helper, the
agent-task command runner, ``ffmpeg``, and the one-shot probes around them.

The seam lives in ``clear_record.core`` because that is the only depth every
layer may import (``core`` is the one layer each other layer is allowed to
import, and it imports nothing but the standard library). Placed under any layer
above it, the seam would have been unreachable to at least one of the callers
above — which is why each of them used to hand-roll its own ``subprocess`` call.

The semantics the callers rely on, kept together here:

- **Scoped cancellation.** Cancellation belongs to one *runner*, not to the
  process, and it is scoped to that runner's children and no others.
  :meth:`CancellableProcessRunner.cancel` is the *pool's own* flag: it makes
  later launches fail fast with :class:`ProcessCancelled` and does **not** reach a
  child that is already running (the pool sets it in its interruption handler,
  beside the :meth:`~CancellableProcessRunner.terminate_all` that stops its
  children). A child already running is stopped by the run's own signal (RUN-04),
  which :meth:`~CancellableProcessRunner.run` acts on where the child's handle
  is, or by :meth:`~CancellableProcessRunner.terminate_all` /
  :meth:`~CancellableProcessRunner.stop`.
- **A grace period.** A child asked to stop (:meth:`~CancellableProcessRunner.
  terminate_all`, :meth:`~CancellableProcessRunner.stop`) gets
  :data:`DEFAULT_CANCEL_GRACE_S` — or the caller's own grace, which is what a
  caller with a different ladder passes — before it is hard-killed.
- **Live-child tracking.** :meth:`~CancellableProcessRunner.live_pids` lists
  exactly the children this runner launched and has not reaped, which is what
  the transcribe pool samples decoder memory through.
- **Terminating only its own children.** A runner touches the children it
  launched through itself; a second runner's child, or an unrelated process on
  the machine, is never signalled.

The design this replaces: monkey-patching ``subprocess.Popen.__init__`` for the
pool's lifetime leaked into unrelated subprocesses and was unsafe when
``clear_record.cli`` is embedded or two pools run in one process, so the pool
passes a :class:`CancellableProcessRunner` *into* the backend, which launches
every child through it.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Protocol

# How long a terminated child has to exit before it is hard-killed.
DEFAULT_CANCEL_GRACE_S = 1.0


class ProcessCancelled(RuntimeError):
    """Raised when a runner refuses a launch after cancellation, or when the
    run's own cancel signal (RUN-04) arrives while a launched child is still
    running."""


class ProcessRunner(Protocol):
    """Launch a child process and capture its output.

    A structural match for the subset of ``subprocess.run`` the callers use, so
    the default :class:`SubprocessRunner` is a drop-in and tests can inject a
    fake. ``check``, ``cwd`` and ``env`` carry ``subprocess.run``'s meanings
    unchanged: a non-zero exit raises :class:`subprocess.CalledProcessError`
    when ``check`` is set, ``cwd`` selects the child's working directory and
    ``env`` replaces its environment (``None`` inherits).
    """

    def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
        check: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
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
        check: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
            cwd=cwd,
            env=env,
        )


#: How long a launch waits before looking at the cancel signals again. Small
#: enough that a cancelled run stops promptly, long enough that the poll costs
#: nothing beside a child that runs for seconds.
_CANCEL_POLL_S = 0.1


class CancellableProcessRunner:
    """Track this runner's children so their owner can terminate them on cancel.

    One instance per operation: :meth:`terminate_all` and :meth:`stop` only
    touch children started through this instance, so a second concurrent
    operation — or an unrelated process on the machine — is unaffected.
    Instances are safe to share across an operation's worker threads.

    ``cancel`` is the *run's* cancel signal (RUN-04), when the operation belongs
    to a run: an event another thread sets — the console asking a run to stop. It
    is read through :attr:`cancelled` beside this runner's own flag, so the
    transcribe pool's existing checks need no second condition: a requested
    cancel ends the chunk in flight exactly as its Ctrl-C does.
    """

    def __init__(self, cancel: threading.Event | None = None) -> None:
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._cancel = cancel

    @property
    def cancelled(self) -> bool:
        """Whether a cancellation was requested: this runner's, or the run's."""
        return self._cancelled.is_set() or self.run_cancelled

    @property
    def run_cancelled(self) -> bool:
        """Whether the **run** asked this operation to stop (RUN-04).

        Deliberately distinct from :attr:`cancelled`, which also covers this
        runner's own flag — the one the transcribe pool sets in its interruption
        handler while it stops its children. Only the run's own signal means
        "report this chunk as cancelled": the pool's flag must leave the backends
        seeing exactly what they saw before it existed, a child stopped out from
        under them, which is how the CLI's Ctrl-C reports itself.
        """
        return self._cancel is not None and self._cancel.is_set()

    def cancel(self) -> None:
        """Set this runner's own flag: later launches fail fast (see ``run``).

        It does not reach a child that is already running. The pool sets it in
        its interruption handler, beside the ``terminate_all`` that stops those
        children — a backend parked in ``communicate`` must keep seeing its child
        stopped out from under it, which is the behaviour this flag preserves.
        """
        self._cancelled.set()

    def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float | None = None,
        check: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        if self.cancelled:
            raise ProcessCancelled("process runner cancelled before launch")
        proc = self._spawn(
            cmd, capture_output=capture_output, text=text, cwd=cwd, env=env
        )
        try:
            out, err = self._wait(proc, cmd, timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self._forget(proc)
            raise
        except ProcessCancelled:
            # The run's own signal arrived while this child was running (RUN-04):
            # it is terminated here, where its handle is, rather than leaving the
            # wait to run its course — the pool reports that chunk as cancelled,
            # not as a failure of the child.
            self._terminate(proc)
            self._forget(proc)
            raise
        except BaseException:
            # Ctrl-C in the main thread can interrupt communicate(); make sure
            # the child dies before the exception propagates. In the pool path a
            # worker is parked in communicate(), so terminate_all() (not this
            # branch) stops the child.
            self._terminate(proc)
            self._forget(proc)
            raise
        self._forget(proc)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def start(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen:
        """Launch a **long-lived** child and return its live handle, tracked.

        The counterpart of :meth:`run` for a child that is meant to outlive the
        call — the console's foreground ``tailscale serve`` blocks while the
        console runs, so there is nothing to wait for. The child is tracked from
        this launch until :meth:`stop` ends it, so :meth:`live_pids` sees it
        while it runs and only this runner may signal it.

        A cancelled runner refuses the launch, exactly as ``run`` does.
        """
        if self.cancelled:
            raise ProcessCancelled("process runner cancelled before launch")
        return self._spawn(
            cmd, capture_output=capture_output, text=text, cwd=cwd, env=env
        )

    def stop(
        self, proc: subprocess.Popen, *, grace: float = DEFAULT_CANCEL_GRACE_S
    ) -> None:
        """Ask one child this runner started to exit; hard-kill it after ``grace``.

        The single-child form of :meth:`terminate_all`, for a caller that owns
        exactly one long-lived child: SIGTERM, then ``grace`` seconds for it to
        leave on its own, then SIGKILL and a second bounded wait. The handle is
        forgotten either way, so a stopped child is never re-signalled by a later
        call. A child that has already exited is left alone (its status is simply
        reaped).
        """
        try:
            if proc.poll() is None:
                self._terminate(proc)
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    self._kill(proc)
                    try:
                        proc.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._forget(proc)

    def _spawn(
        self,
        cmd: list[str],
        *,
        capture_output: bool,
        text: bool,
        cwd: str | os.PathLike[str] | None,
        env: Mapping[str, str] | None,
    ) -> subprocess.Popen:
        """Launch the child and track it, from before anyone can wait on it."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            cwd=cwd,
            env=env,
        )
        with self._lock:
            self._procs.append(proc)
        return proc

    def _wait(
        self, proc: subprocess.Popen, cmd: list[str], timeout: float | None
    ) -> tuple[str | None, str | None]:
        """Wait for a child, honouring a cancel that arrives while it runs.

        ``communicate`` blocks until the child exits, so on its own it can only
        notice a cancellation *before* a launch or *after* the results are in.
        This waits in short slices instead and raises :class:`ProcessCancelled`
        as soon as the run asks to stop — which is what lets that stop reach a
        child that is already running, rather than waiting for it to return. The
        caller's own ``timeout`` still means exactly what it meant: an expired
        deadline raises ``subprocess.TimeoutExpired``.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.run_cancelled:
                raise ProcessCancelled("process runner cancelled while a child ran")
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
        this: a child is listed from its launch until it is reaped — when
        :meth:`run`'s wait on it returns, or when :meth:`stop` ends a child
        launched through :meth:`start` — and only this runner launched it, so a
        caller samples exactly one pool's work and never an unrelated process. A
        child that has already exited is reaped here and left out, so a caller
        can never read a zombie's empty /proc entry and mistake it for zero
        memory.
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
        if not self._await_exit(procs, grace):
            for proc in procs:
                self._kill(proc)

    @staticmethod
    def _await_exit(procs: list[subprocess.Popen], grace: float) -> bool:
        """Whether every child exited within ``grace`` (checked as it elapses)."""
        deadline = time.monotonic() + grace
        while True:
            if all(proc.poll() is not None for proc in procs):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

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
