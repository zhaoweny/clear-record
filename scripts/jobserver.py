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
the last descriptor frees the FIFO's in-memory pipe, buffered tokens included,
so ``init`` leaves a detached **keeper** process holding the pipe open for the
machine's (user session's) lifetime, records its pid beside the pipe, and reuses
a pipe only while that keeper answers.

**A pipe is never unlinked while anyone still holds it.** A worker parked on a
replaced path can never be woken — its descriptor points at the old inode, and
no keeper will ever seed it again. So when the path exists without a live keeper
``init`` *attaches* a keeper to the same inode if any process holds it open, and
only replaces the path when nobody does. A keeper is recycled only when no run
is live; ``init`` makes that recycling safe rather than forbidden.

**A token also dies with a worker killed while holding one.** The keeper probes:
a token it finds goes straight back (no net change), and a pipe it has watched
empty for several consecutive probes is topped up. The top-up writes only what
the pool is missing — ``tokens`` minus the processes holding the pipe — so
attaching to a live pool cannot inflate it, and an empty pool with no holders
comes back whole.

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
import select
import signal
import stat
import struct
import subprocess
import sys
import termios
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

#: How often the keeper probes the pool, and how many consecutive empty probes
#: mean "this pool leaked" rather than "every token is briefly checked out".
PROBE_SECONDS = 15.0
EMPTY_PROBES_BEFORE_RESEED = 4


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


def holders(path: Path) -> set[int]:
    """The pids holding this pipe open, found through ``/proc`` (empty with no procfs)."""
    try:
        inode = path.stat().st_ino
        entries = os.listdir("/proc")
    except OSError:
        return set()
    mine = os.getpid()
    found: set[int] = set()
    for entry in entries:
        if not entry.isdigit() or int(entry) == mine:
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.stat(f"{fd_dir}/{fd}").st_ino == inode:
                    found.add(int(entry))
                    break
            except OSError:
                continue
    return found


def _pool_size(fd: int) -> int | None:
    """The tokens currently in the pipe, or ``None`` where the ioctl is unsupported."""
    try:
        buf = fcntl.ioctl(fd, termios.FIONREAD, struct.pack("I", 0))
    except OSError:
        return None
    return struct.unpack("I", buf)[0]


def top_up(path: Path, fd: int, tokens: int) -> None:
    """Write the tokens the pool is missing, and only those.

    A pool with tokens in it has not leaked — every byte in flight is one a
    worker holds — so this is a no-op. An empty pool is sized against the
    processes holding the pipe: a keeper attaching to a live pool writes
    ``tokens`` minus those holders, and a drained pool with no holders comes
    back whole.
    """
    if _pool_size(fd):
        return
    missing = tokens - len(holders(path))
    if missing > 0:
        os.write(fd, TOKEN * missing)


def keep(
    path: Path,
    tokens: int,
    probe_seconds: float,
    empty_probes: int,
) -> int:
    """Hold ``path`` open, seed what it needs, and probe — the keeper itself."""
    fd = os.open(path, os.O_RDWR)  # holds the pipe object alive
    os.set_blocking(fd, False)  # a probe must never park behind another reader
    top_up(path, fd, tokens)
    pid_path(path).write_text(f"{os.getpid()}\n")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    empty = 0
    while True:
        readable, _, _ = select.select([fd], [], [], probe_seconds)
        if readable:
            try:
                token = os.read(fd, 1)
            except BlockingIOError:
                # Another reader took the byte between select and read: the pool
                # held one, and blocking here is how the keeper would hang.
                empty = 0
            else:
                if token:
                    os.write(fd, token)  # straight back: the probe changes nothing
                    empty = 0
            time.sleep(probe_seconds)  # pace: a full pool must not spin
            continue
        empty += 1
        if empty >= empty_probes:
            top_up(path, fd, tokens)  # leak recovery, sized by the holders
            empty = 0


def spawn_keeper(path: Path, tokens: int) -> bool:
    """Start a detached keeper and wait until it has seeded or attached."""
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
                # No live keeper. A pipe someone still holds keeps its inode: the
                # pool and every parked worker live there, so attach to it rather
                # than replace it. Only a pipe nobody holds is safely rebuilt.
                if holders(path):
                    if not spawn_keeper(path, tokens):
                        raise OSError("keeper did not attach in time")
                    return path
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
    keep_command.add_argument("--probe-seconds", type=float, default=PROBE_SECONDS)
    keep_command.add_argument(
        "--empty-probes", type=int, default=EMPTY_PROBES_BEFORE_RESEED
    )
    args = parser.parse_args(argv)

    if args.command == "keep":
        return keep(
            args.path.resolve(),
            max(1, args.tokens),
            args.probe_seconds,
            max(1, args.empty_probes),
        )
    path = init((args.path or default_path()).resolve(), max(1, args.tokens))
    if path is None:
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
