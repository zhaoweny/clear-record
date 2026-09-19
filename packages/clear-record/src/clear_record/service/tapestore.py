"""The one owner of a meeting's tape storage: the files and the rows naming them.

A meeting's tapes are two things at once — files in the workspace (or in an
archive) and rows in the registry — and the rules binding the two used to be
copied per caller: the upload path and the archive copier each declared the
tapes directory name and each re-derived the filename-collision rule, agreeing
only by a comment on one of them.

This module owns those rules once:

- :data:`TAPES_DIRNAME` and :func:`tapes_dir` — where the tapes live;
- :func:`unique_name` and :func:`unique_path` — the collision rule: a name the
  caller's own notion of "taken" already claims is never handed back;
- :func:`latest_set`, :func:`write_set`, :func:`record_tape` and
  :func:`forget_tape` — the row-plus-tape-set edit, both directions.

It owns the *rules*, not the callers. The upload path keeps its guard order
(the upload id, the declared size, free space, then the filename and extension)
and its own atomicity — a scratch file it fsyncs and renames — and the archive
copier keeps its copy-then-manifest discipline; neither re-derives anything
here. The connection, and so the transaction, stays the caller's: these
functions only write through it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from clear_record.service.models import Tape

#: The tapes directory inside a managed workspace or an archive. In a workspace
#: it is not one of ``Workspace``'s own directories and not in ``SKIP_DIRS``, so
#: ``discover_audio`` finds the tapes: to the pipeline an upload is an input
#: recording like any other.
TAPES_DIRNAME = "tapes"


def tapes_dir(parent: Path) -> Path:
    """The tapes directory under a meeting's workspace, or under an archive."""
    return parent / TAPES_DIRNAME


def unique_name(name: str, *, taken: Callable[[str], bool]) -> str:
    """``name``, or the first free ``stem-N.suffix``: ``a.wav`` -> ``a-2.wav``.

    ``taken`` is the caller's own notion of occupied — the upload path probes the
    filesystem, the archive copier consults the names it has already copied into
    a fresh directory — and it is consulted before anything is written. The
    disambiguation itself is the same for both, so a namesake is either never
    handed back or handed back under the same suffix, whichever path asked.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = name
    n = 2
    while taken(candidate):
        candidate = f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def unique_path(directory: Path, name: str) -> Path:
    """``directory/name``, or its first ``-N`` sibling not already on disk.

    A name that is already there — a file, a directory, or a symlink, a broken
    one included — is never handed back: the caller must not write over it, and
    must not write *through* a link.
    """
    return directory / unique_name(name, taken=_on_disk(directory))


def _on_disk(directory: Path) -> Callable[[str], bool]:
    def taken(name: str) -> bool:
        candidate = directory / name
        return candidate.exists() or candidate.is_symlink()

    return taken


def latest_set(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    """The meeting's latest tape set row, or ``None`` when it has no tapes.

    A tape set is appended, never rewritten: the newest row is the tape set the
    pipeline reads, and the caller maps it to whatever type it owns.
    """
    return conn.execute(
        "SELECT * FROM recording_set WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
        (meeting_id,),
    ).fetchone()


def write_set(
    conn: sqlite3.Connection, meeting_id: int, paths: list[str], created_at: str
) -> int:
    """Append a tape set row holding ``paths``; returns the new row's id."""
    cur = conn.execute(
        "INSERT INTO recording_set (meeting_id, paths, created_at) VALUES (?, ?, ?)",
        (meeting_id, json.dumps(paths), created_at),
    )
    return int(cur.lastrowid)


def record_tape(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    path: str,
    sha256: str,
    size: int,
    created_at: str,
) -> int:
    """Insert a tape row and add ``path`` to the meeting's tape set.

    One transaction — the caller's — so the integrity facts (``sha256``, ``size``)
    and the tape set the pipeline reads can never disagree: the path joins the
    latest set (a first one is created) and the row is written after it. Returns
    the new tape row's id.
    """
    paths = _paths(latest_set(conn, meeting_id))
    if path not in paths:
        paths.append(path)
    write_set(conn, meeting_id, paths, created_at)
    cur = conn.execute(
        "INSERT INTO tape (meeting_id, path, sha256, bytes, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (meeting_id, path, sha256, size, created_at),
    )
    return int(cur.lastrowid)


def forget_tape(conn: sqlite3.Connection, tape: Tape, *, created_at: str) -> None:
    """Delete ``tape``'s row and take its path out of the meeting's tape set.

    The file is the caller's to unlink (nothing here owns a filesystem). Dropping
    the meeting's last tape clears its tape set rather than leaving one pointing
    at a file that no longer exists.
    """
    conn.execute("DELETE FROM tape WHERE id = ?", (tape.id,))
    latest = latest_set(conn, tape.meeting_id)
    if latest is None:
        return
    remaining = [p for p in _paths(latest) if p != tape.path]
    if remaining:
        write_set(conn, tape.meeting_id, remaining, created_at)
    else:
        conn.execute(
            "DELETE FROM recording_set WHERE meeting_id = ?", (tape.meeting_id,)
        )


def _paths(row: sqlite3.Row | None) -> list[str]:
    """The paths a tape-set row holds (nothing, when there is no set)."""
    return list(json.loads(row["paths"])) if row is not None else []


__all__ = [
    "TAPES_DIRNAME",
    "forget_tape",
    "latest_set",
    "record_tape",
    "tapes_dir",
    "unique_name",
    "unique_path",
    "write_set",
]
