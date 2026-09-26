#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Seed the machine-local posix jobserver the test recipes throttle through.

Several checkouts checking at once each run ``pytest -n auto``, so three
concurrent gates put three sets of sixteen workers on sixteen cores. A posix
jobserver caps the **aggregate**: every pytest run handed the same pipe holds
one token per test it executes, and ``just verify`` / ``just test`` hand it the
pipe this command prints.

One pipe serves the machine, not the checkout — the tokens are what concurrent
checkouts contend for — so the path lives under ``XDG_RUNTIME_DIR`` (a per-user
``/tmp`` path when there is none) and is keyed to the user id.

**A pipe's tokens live exactly as long as some process holds it open.** Closing
the seeding descriptor frees the FIFO's in-memory pipe, buffered tokens
included, so ``init`` leaves a detached **keeper** process holding the pipe open
for the machine's (user session's) lifetime, records its pid beside the pipe,
and reuses a pipe only while that keeper answers. A pipe whose keeper is gone is
stale — its tokens are gone with it — and is replaced. Over-seeding from a
benign init race would inflate the pool, so a lock file serialises seeding.

**Best-effort by design**: a convenience that cannot be set up must not fail
the gate it speeds up. A failure prints nothing to stdout, which leaves
``PYTEST_JOBSERVER`` empty and the run unthrottled, and one line to stderr.

``init`` never reseeds a pipe a live keeper holds. Stop the keeper (it holds a
pipe, not a service) or delete the pipe, then run ``init`` again to reseed with
a different ``--tokens``.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

#: One token per CPU is the unbottlenecked machine; several concurrent runs
#: share that one pool instead of each claiming it.
DEFAULT_TOKENS = os.cpu_count() or 1

#: One jobserver token, the byte make and cargo also write (see the POSIX
#: jobserver protocol); the plugin reads one byte per test and writes it back.
TOKEN = b"+"

#: How long ``init`` waits for a fresh keeper to prove it seeded the pipe.
KEEPER_START_TIMEOUT_SECONDS = 5.0


def default_path() -> Path:
    """The pipe's home: the runtime dir when one is set, else a per-user temp path."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp")
    return base / f"clear-record-jobserver-{getattr(os, 'getuid', lambda: 0)()}"


def pid_path(path: Path) -> Path:
    return path.with_name(path.name + ".pid")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def keeper_pid(path: Path) -> int | None:
    """The pid of the live keeper beside ``path``, or ``None``."""
    try:
        pid = int(pid_path(path).read_text().strip())
    except (OSError, ValueError):
        return None
    if not _alive(pid):
        return None
    # Liveness alone is not enough: the pid may since belong to another process.
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return pid  # no procfs: liveness is the strongest check available
    parts = command.split(b"\0")
    if any(part.endswith(b"jobserver.py") for part in parts) and b"keep" in parts:
        return pid
    return None


def keep(path: Path, tokens: int) -> int:
    """Hold ``path`` open and seed it — the keeper process itself."""
    fd = os.open(path, os.O_RDWR)  # holds the pipe object alive
    written = os.write(fd, TOKEN * tokens)
    if written != tokens:
        raise OSError(f"short seed write: {written} of {tokens} bytes")
    pid_path(path).write_text(f"{os.getpid()}\n")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    while True:
        signal.pause()  # hold the descriptor until the machine goes away


def spawn_keeper(path: Path, tokens: int) -> bool:
    """Start a detached keeper and wait until it has seeded the pipe."""
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "keep",
            "--path",
            str(path),
            "--tokens",
            str(tokens),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # no controlling terminal: survives the shell
    )
    deadline = time.monotonic() + KEEPER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if keeper_pid(path) is not None:
            return True
        time.sleep(0.05)
    return False


def init(path: Path, tokens: int) -> Path | None:
    """Ensure a seeded pipe with a live keeper exists; return it, or ``None``."""
    created = False
    try:
        with open(path.with_name(path.name + ".lock"), "w") as lock:  # serialises inits
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                if not stat.S_ISFIFO(path.stat().st_mode):
                    raise OSError(f"{path} exists and is not a fifo")
                if keeper_pid(path) is not None:
                    return path
                # A pipe without a live keeper lost its tokens: replace it.
                path.unlink(missing_ok=True)
            pid_path(path).unlink(missing_ok=True)
            os.mkfifo(path, 0o600)
            created = True
            if not spawn_keeper(path, tokens):
                raise OSError("keeper did not seed the pipe in time")
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        print(f"jobserver: not seeded: {exc}", file=sys.stderr)
        return None
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    init_command = commands.add_parser(
        "init", help="seed the pipe once; print its path (nothing on failure)"
    )
    init_command.add_argument("--path", type=Path, default=None)
    init_command.add_argument("--tokens", type=int, default=DEFAULT_TOKENS)
    keep_command = commands.add_parser("keep", help="hold the pipe open (internal)")
    keep_command.add_argument("--path", type=Path, required=True)
    keep_command.add_argument("--tokens", type=int, default=DEFAULT_TOKENS)
    args = parser.parse_args(argv)

    if args.command == "keep":
        return keep(args.path.resolve(), max(1, args.tokens))
    path = init((args.path or default_path()).resolve(), max(1, args.tokens))
    if path is None:
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
