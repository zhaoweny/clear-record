"""The SQLite registry: projects and their glossary terms.

One database (stdlib :mod:`sqlite3`, no dependency) holds the **app-owned**
project/glossary state under the platform data directory (ADR-0025). Audio,
workspaces and archives stay as files elsewhere; the registry stores metadata
only (ADR-0007/ADR-0013).

Design notes:

- **Opening a connection per operation** keeps the store safe to call from the
  web app's threadpool without sharing a connection across threads. SQLite
  connections are cheap; correctness beats pooling here.
- **Forward-only migration** behind a ``schema_version`` table. A database from a
  newer version fails loudly rather than being silently misread.
- **Slugs are stable, human-readable ids.** A project is addressable by slug in
  the API, the GUI and any agent context; ids stay internal.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clear_record.core.events import JobEvent
from clear_record.service import tapestore
from clear_record.service.models import (
    MEETING_STATUSES,
    RUN_ORIGINS,
    RUN_STATUSES,
    TERM_AUTHORS,
    TERM_STATUSES,
    Archive,
    Artifact,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
    Tape,
)
from clear_record.service.paths import registry_path

SCHEMA_VERSION = 8

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    notes                TEXT NOT NULL DEFAULT '',
    default_archive_root TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glossary_term (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    term        TEXT NOT NULL,
    reading     TEXT,
    aliases     TEXT,
    definition  TEXT,
    status      TEXT NOT NULL DEFAULT 'candidate',
    added_by    TEXT NOT NULL DEFAULT 'human',
    notes       TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, term)
);

CREATE INDEX IF NOT EXISTS glossary_term_project ON glossary_term (project_id);
"""

# v2 — the meeting/run/artifact spine: a project's meetings, the tapes chosen
# for each, the pipeline runs against them, and the artifacts they produce.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meeting (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug           TEXT NOT NULL,
    title          TEXT NOT NULL,
    recorded_at    TEXT,
    workspace_path TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    created_at     TEXT NOT NULL,
    UNIQUE (project_id, slug)
);

CREATE TABLE IF NOT EXISTS recording_set (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    paths      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id    INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    backend       TEXT,
    model         TEXT,
    language      TEXT,
    options       TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    error         TEXT,
    progress      TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    run_id       INTEGER REFERENCES pipeline_run(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT,
    bytes        INTEGER,
    produced_by  TEXT NOT NULL DEFAULT 'pipeline',
    review_state TEXT NOT NULL DEFAULT 'final',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS meeting_project ON meeting (project_id);
CREATE INDEX IF NOT EXISTS pipeline_run_meeting ON pipeline_run (meeting_id);
CREATE INDEX IF NOT EXISTS artifact_meeting ON artifact (meeting_id);
"""

# v3 — the archive spine: each immutable, checksummed copy of a meeting's tapes
# and record. The files live in the user's archive root; the registry stores the
# copy's paths and the manifest's checksum (ADR-0006/ADR-0007).
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS archive_meeting ON archive (meeting_id);
"""

# v4 — the story: meetings gain a free-text ``notes`` column so the user's
# narrative (and an agent's draft) has a durable home beside the glossary
# (ticket 21's tuning loop). Project notes already exist from v1.
_SCHEMA_V4 = """
ALTER TABLE meeting ADD COLUMN notes TEXT NOT NULL DEFAULT '';
"""

# v5 — the managed workspace (ADR-0024): a tape uploaded to the node is recorded
# with the copy's integrity facts (``sha256`` and ``bytes``), which the path-only
# recording set cannot carry. The tape's path is also added to the meeting's
# recording set, so the pipeline (runs/archive) reads an upload exactly like a
# user-typed path.
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS tape (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tape_meeting ON tape (meeting_id);
"""

# v6 — durable run state and the node queue: the live event stream is persisted
# per run (so the run view replays after a restart), and a run records the
# resolved options it will execute with (so a queued run survives a restart and
# is picked back up). Runs are ordered by id as the node's FIFO.
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS run_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS run_event_run ON run_event (run_id);

ALTER TABLE pipeline_run ADD COLUMN run_options TEXT;
"""

# RUN-02: who a run belongs to. ``origin`` is the surface that started it;
# ``owner`` the process that claimed it (the conditional transition that makes
# the queue cross-process); ``heartbeat_at`` the owner's last proof of life,
# refreshed while it executes. All three are NULL for a run recorded before this
# version — read as unknown, never guessed.
_SCHEMA_V7 = """
ALTER TABLE pipeline_run ADD COLUMN origin TEXT;

ALTER TABLE pipeline_run ADD COLUMN owner TEXT;

ALTER TABLE pipeline_run ADD COLUMN heartbeat_at TEXT;
"""

# RUN-04: cancel and resume. ``resumes_run_id`` links a run to the run it
# continues (the chunk cache is what actually makes it cheaper; this is what makes
# the link visible). ``cancel_requested_at`` is a *request* on a ``running`` run:
# only its owner may end it, so the column records who asked and when, and the
# owner's next look at the row turns it into the terminal ``stopped``. Both are
# NULL for a run that neither resumes nor was asked to stop.
_SCHEMA_V8 = """
ALTER TABLE pipeline_run ADD COLUMN resumes_run_id INTEGER;

ALTER TABLE pipeline_run ADD COLUMN cancel_requested_at TEXT;
"""

# Forward-only: each entry is (version it produces, DDL). A fresh registry runs
# them all; an existing one runs only those newer than its stored version.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
    (7, _SCHEMA_V7),
    (8, _SCHEMA_V8),
)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _require_slug(slug: str, what: str) -> str:
    """Validate an explicitly supplied slug: [a-z0-9-]+ only.

    A generated slug is safe by construction; a caller-supplied one is not. The
    managed workspace builds a filesystem path from a meeting's project_slug and
    slug (ADR-0024), so a separator or a '..' component here would escape the
    managed root.
    """
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"{what} slug must match [a-z0-9-]+ (got {slug!r})")
    return slug


class Registry:
    """The single owner of the SQLite registry and its read/writes."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._migrate(conn)

    @classmethod
    def open(
        cls,
        data_dir: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> Registry:
        """Open (creating if needed) the registry at the resolved location."""
        return cls(db_path if db_path is not None else registry_path(data_dir))

    # --- connection / schema ---------------------------------------------- #
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for one operation, committed on a clean exit.

        ``timeout`` is left at :mod:`sqlite3`'s default, which is also the busy
        timeout: a second *process* (the console and an agent's MCP server share
        one registry, RUN-02) that meets a writer waits for it instead of raising
        ``database is locked``. The one shape SQLite refuses to wait for is a
        write that has to upgrade a read transaction, so every write here is
        write-first — one statement, or a write before any read on that
        connection.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if exists is None:
            for _, ddl in _MIGRATIONS:
                conn.executescript(ddl)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            return
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        version = int(row["version"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"registry schema version {version} is newer than this build "
                f"supports ({SCHEMA_VERSION}); upgrade clear-record"
            )
        for target, ddl in _MIGRATIONS:
            if target > version:
                conn.executescript(ddl)
        if version < SCHEMA_VERSION:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    # --- projects ---------------------------------------------------------- #
    def _unique_slug(self, conn: sqlite3.Connection, name: str) -> str:
        base = _slugify(name)
        slug, n = base, 2
        while conn.execute("SELECT 1 FROM project WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def create_project(
        self,
        name: str,
        notes: str = "",
        default_archive_root: str | None = None,
        slug: str | None = None,
    ) -> Project:
        name = name.strip()
        if not name:
            raise ValueError("project name must not be blank")
        explicit = (slug or "").strip()
        if explicit:
            _require_slug(explicit, "project")
        with self._connect() as conn:
            slug = explicit or self._unique_slug(conn, name)
            try:
                cur = conn.execute(
                    "INSERT INTO project (slug, name, notes, default_archive_root, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (slug, name, notes, default_archive_root, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"project slug {slug!r} already exists") from exc
            return self._project_row(conn, cur.lastrowid)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, slug: str) -> Project | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM project WHERE slug = ?", (slug,)
            ).fetchone()
        return self._project(row) if row else None

    def require_project(self, slug: str) -> Project:
        project = self.get_project(slug)
        if project is None:
            raise KeyError(slug)
        return project

    def update_project(
        self,
        slug: str,
        *,
        name: str | None = None,
        notes: str | None = None,
        default_archive_root: str | None = None,
    ) -> Project:
        fields: dict[str, object] = {}
        if name is not None:
            if not name.strip():
                raise ValueError("project name must not be blank")
            fields["name"] = name.strip()
        if notes is not None:
            fields["notes"] = notes
        if default_archive_root is not None:
            fields["default_archive_root"] = default_archive_root
        with self._connect() as conn:
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                cur = conn.execute(
                    f"UPDATE project SET {assignments} WHERE slug = ?",
                    (*fields.values(), slug),
                )
                if cur.rowcount == 0:
                    raise KeyError(slug)
            return self._project_row_by_slug(conn, slug)

    def term_counts(self) -> dict[str, int]:
        """Per-project term count, keyed by project slug (for list views)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.slug AS slug, COUNT(t.id) AS n FROM project p"
                " LEFT JOIN glossary_term t ON t.project_id = p.id"
                " GROUP BY p.id"
            ).fetchall()
        return {row["slug"]: int(row["n"]) for row in rows}

    # --- glossary ---------------------------------------------------------- #
    def add_term(
        self,
        project_slug: str,
        term: str,
        *,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str = "candidate",
        added_by: str = "human",
        notes: str | None = None,
    ) -> GlossaryTerm:
        term = term.strip()
        if not term:
            raise ValueError("term must not be blank")
        if status not in TERM_STATUSES:
            raise ValueError(f"status must be one of {TERM_STATUSES}, got {status!r}")
        if added_by not in TERM_AUTHORS:
            raise ValueError(
                f"added_by must be one of {TERM_AUTHORS}, got {added_by!r}"
            )
        project = self.require_project(project_slug)
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO glossary_term"
                    " (project_id, term, reading, aliases, definition, status, added_by, notes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project.id,
                        term,
                        reading,
                        aliases,
                        definition,
                        status,
                        added_by,
                        notes,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"term {term!r} already exists in {project_slug!r}"
                ) from exc
            return self._term_row(conn, cur.lastrowid)

    def list_terms(
        self, project_slug: str | None = None, status: str | None = None
    ) -> list[GlossaryTerm]:
        if status is not None and status not in TERM_STATUSES:
            raise ValueError(f"status must be one of {TERM_STATUSES}, got {status!r}")
        clauses: list[str] = []
        params: list[object] = []
        if project_slug is not None:
            clauses.append("p.slug = ?")
            params.append(project_slug)
        if status is not None:
            clauses.append("t.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.*, p.slug AS project_slug FROM glossary_term t"
                " JOIN project p ON p.id = t.project_id"
                f" {where}"
                " ORDER BY p.name COLLATE NOCASE, t.term COLLATE NOCASE",
                params,
            ).fetchall()
        return [self._term(row) for row in rows]

    def update_term(
        self,
        term_id: int,
        *,
        term: str | None = None,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> GlossaryTerm:
        fields: dict[str, object] = {}
        if term is not None:
            if not term.strip():
                raise ValueError("term must not be blank")
            fields["term"] = term.strip()
        for key, value in (
            ("reading", reading),
            ("aliases", aliases),
            ("definition", definition),
            ("notes", notes),
        ):
            if value is not None:
                fields[key] = value
        if status is not None:
            if status not in TERM_STATUSES:
                raise ValueError(
                    f"status must be one of {TERM_STATUSES}, got {status!r}"
                )
            fields["status"] = status
        with self._connect() as conn:
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                try:
                    cur = conn.execute(
                        f"UPDATE glossary_term SET {assignments} WHERE id = ?",
                        (*fields.values(), term_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        "a term with that spelling already exists"
                    ) from exc
                if cur.rowcount == 0:
                    raise KeyError(term_id)
            return self._term_row(conn, term_id)

    def delete_term(self, term_id: int) -> None:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM glossary_term WHERE id = ?", (term_id,))
            if cur.rowcount == 0:
                raise KeyError(term_id)

    def get_term(self, term_id: int) -> GlossaryTerm | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.*, p.slug AS project_slug FROM glossary_term t"
                " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
                (term_id,),
            ).fetchone()
        return self._term(row) if row else None

    # --- meetings ---------------------------------------------------------- #
    def _unique_meeting_slug(
        self, conn: sqlite3.Connection, project_id: int, title: str
    ) -> str:
        base = _slugify(title)
        slug, n = base, 2
        while conn.execute(
            "SELECT 1 FROM meeting WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ).fetchone():
            slug = f"{base}-{n}"
            n += 1
        return slug

    def create_meeting(
        self,
        project_slug: str,
        title: str,
        *,
        recorded_at: str | None = None,
        workspace_path: str | None = None,
        slug: str | None = None,
    ) -> Meeting:
        project = self.require_project(project_slug)
        title = title.strip()
        if not title:
            raise ValueError("meeting title must not be blank")
        explicit = (slug or "").strip()
        if explicit:
            _require_slug(explicit, "meeting")
        with self._connect() as conn:
            slug = explicit or self._unique_meeting_slug(conn, project.id, title)
            try:
                cur = conn.execute(
                    "INSERT INTO meeting"
                    " (project_id, slug, title, recorded_at, workspace_path, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'new', ?)",
                    (project.id, slug, title, recorded_at, workspace_path, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"meeting slug {slug!r} already exists in {project_slug!r}"
                ) from exc
            return self._meeting_row(conn, cur.lastrowid)

    def list_meetings(self, project_slug: str | None = None) -> list[Meeting]:
        params: list[object] = []
        where = ""
        if project_slug is not None:
            where = "WHERE p.slug = ?"
            params.append(project_slug)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.*, p.slug AS project_slug FROM meeting m"
                " JOIN project p ON p.id = m.project_id"
                f" {where}"
                " ORDER BY COALESCE(m.recorded_at, m.created_at) DESC, m.id DESC",
                params,
            ).fetchall()
        return [self._meeting(row) for row in rows]

    def get_meeting(self, project_slug: str, meeting_slug: str) -> Meeting | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT m.*, p.slug AS project_slug FROM meeting m"
                " JOIN project p ON p.id = m.project_id"
                " WHERE p.slug = ? AND m.slug = ?",
                (project_slug, meeting_slug),
            ).fetchone()
        return self._meeting(row) if row else None

    def require_meeting(self, project_slug: str, meeting_slug: str) -> Meeting:
        meeting = self.get_meeting(project_slug, meeting_slug)
        if meeting is None:
            raise KeyError(f"{project_slug}/{meeting_slug}")
        return meeting

    def meeting_by_id(self, meeting_id: int) -> Meeting | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT m.*, p.slug AS project_slug FROM meeting m"
                " JOIN project p ON p.id = m.project_id WHERE m.id = ?",
                (meeting_id,),
            ).fetchone()
        return self._meeting(row) if row else None

    def set_meeting_status(self, meeting_id: int, status: str) -> Meeting:
        if status not in MEETING_STATUSES:
            raise ValueError(
                f"status must be one of {MEETING_STATUSES}, got {status!r}"
            )
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE meeting SET status = ? WHERE id = ?", (status, meeting_id)
            )
            if cur.rowcount == 0:
                raise KeyError(meeting_id)
            return self._meeting_row(conn, meeting_id)

    def set_meeting_workspace(self, meeting_id: int, workspace_path: str) -> Meeting:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE meeting SET workspace_path = ? WHERE id = ?",
                (workspace_path, meeting_id),
            )
            if cur.rowcount == 0:
                raise KeyError(meeting_id)
            return self._meeting_row(conn, meeting_id)

    def update_meeting(
        self,
        meeting_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
    ) -> Meeting:
        """Update a meeting's title and/or notes (the user/agent's story).

        ``None`` leaves a field untouched; an empty string clears notes. The
        workspace and status change through their own operations.
        """
        fields: dict[str, object] = {}
        if title is not None:
            if not title.strip():
                raise ValueError("meeting title must not be blank")
            fields["title"] = title.strip()
        if notes is not None:
            fields["notes"] = notes
        with self._connect() as conn:
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                cur = conn.execute(
                    f"UPDATE meeting SET {assignments} WHERE id = ?",
                    (*fields.values(), meeting_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(meeting_id)
            return self._meeting_row(conn, meeting_id)

    # --- recording sets ---------------------------------------------------- #
    def set_recording_set(
        self, meeting_id: int, paths: list[str] | tuple[str, ...]
    ) -> RecordingSet:
        clean = [str(p) for p in paths if str(p).strip()]
        if not clean:
            raise ValueError("a recording set needs at least one tape")
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._connect() as conn:
            set_id = tapestore.write_set(conn, meeting_id, clean, _now())
            return self._recording_set_row(conn, set_id)

    def latest_recording_set(self, meeting_id: int) -> RecordingSet | None:
        with self._connect() as conn:
            row = tapestore.latest_set(conn, meeting_id)
        return self._recording_set(row) if row else None

    # --- uploaded tapes ---------------------------------------------------- #
    def register_tape(
        self,
        meeting_id: int,
        *,
        path: str,
        sha256: str,
        bytes: int,
    ) -> Tape:
        """Record an uploaded tape **and** add it to the meeting's tape set.

        One transaction, so the integrity facts and the tape set the pipeline
        reads can never disagree: the path is appended to the latest set (or a
        first set is created), then the tape row is written. The edit is
        :mod:`clear_record.service.tapestore`'s (the one owner of a meeting's
        tape storage); this method owns the guard and the transaction.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._connect() as conn:
            tape_id = tapestore.record_tape(
                conn,
                meeting_id,
                path=path,
                sha256=sha256,
                size=bytes,
                created_at=_now(),
            )
            return self._tape_row(conn, tape_id)

    def list_tapes(self, meeting_id: int) -> list[Tape]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tape WHERE meeting_id = ? ORDER BY id", (meeting_id,)
            ).fetchall()
        return [self._tape(row) for row in rows]

    def get_tape(self, tape_id: int) -> Tape | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tape WHERE id = ?", (tape_id,)).fetchone()
        return self._tape(row) if row else None

    def forget_tape(self, tape_id: int) -> Tape:
        """Drop a tape's row and remove its path from the meeting's tape set.

        The file is the caller's to unlink (the store owns no filesystem). When
        the deleted tape was the meeting's last one, the tape set is cleared
        rather than left pointing at a file that no longer exists. The edit is
        :mod:`clear_record.service.tapestore`'s, in this method's transaction.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tape WHERE id = ?", (tape_id,)).fetchone()
            if row is None:
                raise KeyError(tape_id)
            tape = self._tape(row)
            tapestore.forget_tape(conn, tape, created_at=_now())
            return tape

    # --- pipeline runs ----------------------------------------------------- #
    def create_run(
        self,
        meeting_id: int,
        *,
        backend: str | None = None,
        model: str | None = None,
        language: str | None = None,
        options: dict | None = None,
        run_options: dict | None = None,
        origin: str | None = None,
        resumes_run_id: int | None = None,
    ) -> PipelineRun:
        """Create a **queued** run at the back of the node's FIFO.

        ``options`` is run meta (e.g. the glossary snapshot identity) recorded so
        a re-run can be explained later. ``run_options`` is the resolved
        :class:`~clear_record.core.PipelineOptions` the run will execute with,
        recorded so a queued run is picked back up after a restart. ``origin`` is
        the surface that started it, one of :data:`RUN_ORIGINS` (RUN-02); it is
        ``None`` only for a caller that is not a start path (a seeded row).

        ``resumes_run_id`` links this run to the run it continues (RUN-04). The
        reference is checked here rather than left to the reader: a link to a run
        that does not exist, or to a run of another meeting, would be a lie the
        registry itself could see.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        if origin is not None and origin not in RUN_ORIGINS:
            raise ValueError(f"origin must be one of {RUN_ORIGINS}, got {origin!r}")
        if resumes_run_id is not None:
            previous = self.get_run(resumes_run_id)
            if previous is None:
                raise KeyError(resumes_run_id)
            if previous.meeting_id != meeting_id:
                raise ValueError(
                    f"run {resumes_run_id} belongs to meeting "
                    f"{previous.meeting_id}, not {meeting_id}"
                )
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_run"
                " (meeting_id, status, backend, model, language, options,"
                "  run_options, created_at, origin, resumes_run_id)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    backend,
                    model,
                    language,
                    json.dumps(options) if options else None,
                    json.dumps(run_options) if run_options else None,
                    _now(),
                    origin,
                    resumes_run_id,
                ),
            )
            return self._run_row(conn, cur.lastrowid)

    def update_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        error: str | None = None,
        progress: dict | None = None,
    ) -> PipelineRun:
        """Update a run's mutable fields.

        ``progress`` is the terminal summary written when a run ends. Its
        ``cost`` is the run's raw cost record (RUN-01), which the console and
        the history-based ETA read back through :class:`PipelineRun`.

        ``status='done'`` also **clears** ``error`` (RUN-02): a run that reached
        its own successful end reports no error, whatever a reaper wrote on it
        while wrongly believing the owner dead — the reason would otherwise sit on a
        finished run and be shown as a failure by the console and the API. There
        is no caller that wants both, so the clear wins over a passed ``error``.
        """
        fields: dict[str, object] = {}
        if status is not None:
            if status not in RUN_STATUSES:
                raise ValueError(
                    f"status must be one of {RUN_STATUSES}, got {status!r}"
                )
            fields["status"] = status
        if started_at is not None:
            fields["started_at"] = started_at
        if ended_at is not None:
            fields["ended_at"] = ended_at
        if error is not None:
            fields["error"] = error
        if progress is not None:
            fields["progress"] = json.dumps(progress)
        if status == "done":
            fields["error"] = None
        with self._connect() as conn:
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                cur = conn.execute(
                    f"UPDATE pipeline_run SET {assignments} WHERE id = ?",
                    (*fields.values(), run_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(run_id)
            return self._run_row(conn, run_id)

    def get_run(self, run_id: int) -> PipelineRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_run WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run(row) if row else None

    def list_runs(self, meeting_id: int) -> list[PipelineRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_run WHERE meeting_id = ? ORDER BY id DESC",
                (meeting_id,),
            ).fetchall()
        return [self._run(row) for row in rows]

    # --- the node queue (one run at a time) -------------------------------- #
    def active_run_for_meeting(self, meeting_id: int) -> PipelineRun | None:
        """The meeting's newest run that is ``queued`` or ``running``, if any.

        This is the persisted form of the "one run per meeting" dedupe: it is
        derived from the registry, so it survives a restart where the in-memory
        guard did not.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_run WHERE meeting_id = ?"
                " AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
        return self._run(row) if row else None

    def runs_with_status(
        self, *statuses: str, limit: int | None = None
    ) -> list[PipelineRun]:
        """Every run in one of ``statuses``, oldest first (the FIFO order).

        ``limit`` bounds the query to the **newest** ``limit`` runs — read and
        decoded in the database rather than in the caller — and still returns
        them oldest first, so a caller that only needs recent history (the
        history-based ETA) does not pay for the whole table.
        """
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        if limit is None:
            sql = (
                "SELECT * FROM pipeline_run"
                f" WHERE status IN ({placeholders}) ORDER BY id"
            )
            params: tuple = tuple(statuses)
        else:
            sql = (
                "SELECT * FROM pipeline_run"
                f" WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?"
            )
            params = (*statuses, limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        runs = [self._run(row) for row in rows]
        return runs if limit is None else list(reversed(runs))

    def oldest_queued_run(self) -> PipelineRun | None:
        """The head of the node's FIFO: the oldest run still ``queued``."""
        runs = self.runs_with_status("queued")
        return runs[0] if runs else None

    def finished_runs(
        self, *statuses: str, limit: int | None = None
    ) -> list[PipelineRun]:
        """Runs in ``statuses``, newest **finish** first.

        :meth:`runs_with_status` orders by id, which is *creation* order — and a
        run created later can finish earlier (the queue is FIFO, a cancel is
        not), so "the newest run" and "the run that finished most recently" are
        not the same row. Ranking a status view by ``ended_at`` is what "newest
        finished run" means there: the chip's own state and the history it links
        to have to agree.

        ``ended_at`` is written by every terminal transition, so the
        ``created_at`` fallback only covers a row written by something that did
        not set one; ``id`` breaks a tie between two runs that ended in the same
        second. ``limit`` bounds the query to the newest ``limit`` in that
        order, read and ordered in the database rather than in the caller.
        """
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            "SELECT * FROM pipeline_run"
            f" WHERE status IN ({placeholders})"
            " ORDER BY COALESCE(ended_at, created_at) DESC, id DESC"
        )
        params: tuple = tuple(statuses)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run(row) for row in rows]

    def claim_run(self, run_id: int, *, owner: str) -> PipelineRun | None:
        """Claim a ``queued`` run for ``owner``: the node's one write for ``running``.

        Returns the claimed row, or ``None`` when the claim is lost — either
        another process claimed this run first, or the node already has a
        ``running`` run (the queue's one-at-a-time rule, enforced by the same
        statement rather than by the read that chose the run).

        This is deliberately **one statement**. SQLite's write lock decides the
        winner *inside* it, so two claimants that both read ``queued`` cannot both
        write ``running``. It also means the write lock is taken before any row of
        this run is read: a claimant can never hold a read snapshot from before
        the other's commit, which is the upgrade SQLite refuses to wait for (it
        returns ``SQLITE_BUSY`` without consulting the busy handler). Contention
        therefore waits on the connection's busy timeout instead of failing, and
        a crude read-then-update — the shape this replaces — has no such promise.

        ``started_at`` and the first heartbeat are written here, together with the
        status: a claimed run is provably alive from the instant it is claimed.
        """
        at = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_run"
                " SET status = 'running', started_at = ?, owner = ?, heartbeat_at = ?"
                " WHERE id = ? AND status = 'queued'"
                " AND NOT EXISTS (SELECT 1 FROM pipeline_run WHERE status = 'running')",
                (at, owner, at, run_id),
            )
            if cur.rowcount == 0:
                return None
            return self._run_row(conn, run_id)

    def heartbeat_run(self, run_id: int, *, at: str | None = None) -> bool:
        """Refresh a running run's liveness heartbeat; ``False`` when it did not land.

        A ``False`` is not an error to act on: it means the row is not ``running``
        any more (a peer reaped it, believing the owner dead) or does not exist.
        The executing process keeps working either way — a pipeline has no
        cancellation contract — and its own terminal write is what the run finally
        says. The value returned is for the caller (and its tests) to see which of
        the two happened.
        """
        at = at or _now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_run SET heartbeat_at = ?"
                " WHERE id = ? AND status = 'running'",
                (at, run_id),
            )
            landed = cur.rowcount == 1
        return landed

    def interrupt_run(
        self, run_id: int, *, ended_at: str, error: str, progress: dict
    ) -> PipelineRun | None:
        """Move a ``running`` run whose owner is gone to ``interrupted``.

        The conditional counterpart of :meth:`claim_run`, and conditional for the
        same reason: a reaper decides from a **snapshot** — a heartbeat that had
        gone stale — and writes afterwards, so the owner may have finished and
        recorded its own outcome in between. ``None`` means the transition lost
        that race; the caller then leaves the row (and the meeting) alone,
        because a run that reached its own end is not an orphan, whatever its
        heartbeat said a moment ago.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_run SET status = 'interrupted', ended_at = ?,"
                " error = ?, progress = ?"
                " WHERE id = ? AND status = 'running'",
                (ended_at, error, json.dumps(progress), run_id),
            )
            if cur.rowcount == 0:
                return None
            return self._run_row(conn, run_id)

    def stop_run(
        self, run_id: int, *, ended_at: str, progress: dict
    ) -> PipelineRun | None:
        """Move a **queued** run to ``stopped``, before anyone claimed it (RUN-04).

        The counterpart of :meth:`claim_run` for a run that has not started:
        both are conditional on ``status = 'queued'``, so a cancel and a claim
        cannot both win — whoever loses sees no row and acts on what it finds
        instead. Moving the row out of ``queued`` is what makes a cancellation
        stick: the drain never picks it up, the meeting's active-run guard lets go
        of it, and a restart has nothing to resurrect.

        ``None`` means the run was not ``queued`` any more (claimed or already
        terminal): the caller re-reads it rather than insisting.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_run SET status = 'stopped', ended_at = ?,"
                " progress = ?"
                " WHERE id = ? AND status = 'queued'",
                (ended_at, json.dumps(progress), run_id),
            )
            if cur.rowcount == 0:
                return None
            return self._run_row(conn, run_id)

    def request_cancel(
        self, run_id: int, *, at: str | None = None
    ) -> PipelineRun | None:
        """Record a cancel **request** on a ``running`` run (RUN-04).

        A running run belongs to whoever is executing it, and only that process
        may end it: its pipeline is mid-write in a workspace, so a second process
        flipping the row to a terminal status would record a stop that did not
        happen and let the work continue unheard. This writes the request; the
        owner honours it (it reads this column on every heartbeat), stops at its
        next safe boundary and writes ``stopped`` itself.

        The first request's timestamp is kept, so asking twice is idempotent
        rather than a rewrite. ``None`` means the run was not ``running`` any
        more — already terminal by the time the request arrived.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pipeline_run SET cancel_requested_at ="
                " COALESCE(cancel_requested_at, ?)"
                " WHERE id = ? AND status = 'running'",
                (at or _now(), run_id),
            )
            if cur.rowcount == 0:
                return None
            return self._run_row(conn, run_id)

    def cancel_requested(self, run_id: int) -> bool:
        """Whether a cancel has been requested for this run (RUN-04).

        Read by the executing owner's heartbeat, which is the only place a
        *request* can become an actual stop: cheap, one column, and only asked
        while a run is live.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested_at FROM pipeline_run WHERE id = ?",
                (run_id,),
            ).fetchone()
        return bool(row and row["cancel_requested_at"])

    def queue_position(self, run_id: int) -> int:
        """A queued run's 1-based place in the FIFO (``0`` when not queued).

        Position ``1`` is next: nothing queued before it. The count is the
        registry's, so it is stable across a restart.
        """
        run = self.get_run(run_id)
        if run is None or run.status != "queued":
            return 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS ahead FROM pipeline_run"
                " WHERE status = 'queued' AND id < ?",
                (run_id,),
            ).fetchone()
        return int(row["ahead"]) + 1

    # --- the persisted event stream ---------------------------------------- #
    def add_run_event(self, run_id: int, event: JobEvent) -> int:
        """Append one progress event to a run's durable stream; return its seq.

        The sequence is assigned atomically by the insert, so a run's chunk
        workers can report concurrently and the stream still has one order.
        """
        payload = json.dumps(dataclasses.asdict(event), ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO run_event (run_id, seq, payload, created_at)"
                " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM run_event WHERE run_id = ?",
                (run_id, payload, _now(), run_id),
            )
            row = conn.execute(
                "SELECT seq FROM run_event WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return int(row["seq"])

    def list_run_events(self, run_id: int, after: int = 0) -> list[JobEvent]:
        """A run's persisted events with ``seq`` greater than ``after``, in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM run_event WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after),
            ).fetchall()
        return [self._event(json.loads(row["payload"])) for row in rows]

    def latest_run_event(self, run_id: int) -> JobEvent | None:
        """A run's last persisted event, or ``None`` when it has none.

        The one-event read for a caller that wants a run's *current* stage and
        progress — the console's status page, which renders a live run from the
        registry alone (RUN-03) — rather than its whole stream: a transcribe
        stage persists one event per chunk, so reading them all to keep the last
        is work the reader does not need and cannot bound.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM run_event WHERE run_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._event(json.loads(row["payload"])) if row else None

    def count_run_events(self, run_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM run_event WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _event(payload: dict) -> JobEvent:
        """Rebuild a :class:`JobEvent`, ignoring any field a newer build added."""
        known = {field.name for field in dataclasses.fields(JobEvent)}
        return JobEvent(
            **{key: value for key, value in payload.items() if key in known}
        )

    # --- artifacts --------------------------------------------------------- #
    def add_artifact(
        self,
        meeting_id: int,
        *,
        kind: str,
        path: str,
        run_id: int | None = None,
        sha256: str | None = None,
        bytes: int | None = None,
        produced_by: str = "pipeline",
        review_state: str = "final",
    ) -> Artifact:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO artifact"
                " (meeting_id, run_id, kind, path, sha256, bytes, produced_by, review_state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    run_id,
                    kind,
                    path,
                    sha256,
                    bytes,
                    produced_by,
                    review_state,
                    _now(),
                ),
            )
            return self._artifact_row(conn, cur.lastrowid)

    def list_artifacts(self, meeting_id: int) -> list[Artifact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact WHERE meeting_id = ? ORDER BY kind, id",
                (meeting_id,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def latest_artifact(self, meeting_id: int, kind: str) -> Artifact | None:
        """The meeting's newest artifact of ``kind``, or ``None``.

        "Newest" is the highest id, which is the insertion order: a meeting's
        ``minutes`` artifact is the one most recently accepted, so a re-accept
        supersedes the earlier one without rewriting history.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifact WHERE meeting_id = ? AND kind = ?"
                " ORDER BY id DESC LIMIT 1",
                (meeting_id, kind),
            ).fetchone()
        return self._artifact(row) if row else None

    # --- archives ---------------------------------------------------------- #
    def add_archive(
        self,
        meeting_id: int,
        project_id: int,
        *,
        root_path: str,
        manifest_path: str,
        manifest_sha256: str,
    ) -> Archive:
        """Record an already-written archive directory.

        The files are the caller's job (see
        :func:`clear_record.service.archive.archive_meeting`); the registry only
        remembers where the copy is and how to seal it.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO archive"
                " (meeting_id, project_id, root_path, manifest_path, manifest_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    project_id,
                    root_path,
                    manifest_path,
                    manifest_sha256,
                    _now(),
                ),
            )
            return self._archive_row(conn, cur.lastrowid)

    def list_archives(self, meeting_id: int) -> list[Archive]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive WHERE meeting_id = ? ORDER BY id DESC",
                (meeting_id,),
            ).fetchall()
        return [self._archive(row) for row in rows]

    def get_archive(self, archive_id: int) -> Archive | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM archive WHERE id = ?", (archive_id,)
            ).fetchone()
        return self._archive(row) if row else None

    # --- row mapping ------------------------------------------------------- #
    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(
            id=int(row["id"]),
            slug=row["slug"],
            name=row["name"],
            notes=row["notes"],
            default_archive_root=row["default_archive_root"],
            created_at=row["created_at"],
        )

    def _project_row(self, conn: sqlite3.Connection, project_id: int) -> Project:
        row = conn.execute(
            "SELECT * FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        return self._project(row)

    def _project_row_by_slug(self, conn: sqlite3.Connection, slug: str) -> Project:
        row = conn.execute("SELECT * FROM project WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise KeyError(slug)
        return self._project(row)

    @staticmethod
    def _term(row: sqlite3.Row) -> GlossaryTerm:
        return GlossaryTerm(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            project_slug=row["project_slug"],
            term=row["term"],
            reading=row["reading"],
            aliases=row["aliases"],
            definition=row["definition"],
            status=row["status"],
            added_by=row["added_by"],
            notes=row["notes"],
            created_at=row["created_at"],
        )

    def _term_row(self, conn: sqlite3.Connection, term_id: int) -> GlossaryTerm:
        row = conn.execute(
            "SELECT t.*, p.slug AS project_slug FROM glossary_term t"
            " JOIN project p ON p.id = t.project_id WHERE t.id = ?",
            (term_id,),
        ).fetchone()
        if row is None:
            raise KeyError(term_id)
        return self._term(row)

    @staticmethod
    def _meeting(row: sqlite3.Row) -> Meeting:
        return Meeting(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            project_slug=row["project_slug"],
            slug=row["slug"],
            title=row["title"],
            recorded_at=row["recorded_at"],
            workspace_path=row["workspace_path"],
            notes=row["notes"],
            status=row["status"],
            created_at=row["created_at"],
        )

    def _meeting_row(self, conn: sqlite3.Connection, meeting_id: int) -> Meeting:
        row = conn.execute(
            "SELECT m.*, p.slug AS project_slug FROM meeting m"
            " JOIN project p ON p.id = m.project_id WHERE m.id = ?",
            (meeting_id,),
        ).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return self._meeting(row)

    @staticmethod
    def _recording_set(row: sqlite3.Row) -> RecordingSet:
        return RecordingSet(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            paths=tuple(json.loads(row["paths"])),
            created_at=row["created_at"],
        )

    def _recording_set_row(self, conn: sqlite3.Connection, set_id: int) -> RecordingSet:
        row = conn.execute(
            "SELECT * FROM recording_set WHERE id = ?", (set_id,)
        ).fetchone()
        if row is None:
            raise KeyError(set_id)
        return self._recording_set(row)

    @staticmethod
    def _tape(row: sqlite3.Row) -> Tape:
        return Tape(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            path=row["path"],
            sha256=row["sha256"],
            bytes=int(row["bytes"]),
            created_at=row["created_at"],
        )

    def _tape_row(self, conn: sqlite3.Connection, tape_id: int) -> Tape:
        row = conn.execute("SELECT * FROM tape WHERE id = ?", (tape_id,)).fetchone()
        if row is None:
            raise KeyError(tape_id)
        return self._tape(row)

    @staticmethod
    def _run(row: sqlite3.Row) -> PipelineRun:
        return PipelineRun(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            status=row["status"],
            backend=row["backend"],
            model=row["model"],
            language=row["language"],
            options=json.loads(row["options"]) if row["options"] else None,
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            error=row["error"],
            created_at=row["created_at"],
            run_options=json.loads(row["run_options"]) if row["run_options"] else None,
            progress=json.loads(row["progress"]) if row["progress"] else None,
            origin=row["origin"],
            owner=row["owner"],
            heartbeat_at=row["heartbeat_at"],
            resumes_run_id=row["resumes_run_id"],
            cancel_requested_at=row["cancel_requested_at"],
        )

    def _run_row(self, conn: sqlite3.Connection, run_id: int) -> PipelineRun:
        row = conn.execute(
            "SELECT * FROM pipeline_run WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            run_id=row["run_id"],
            kind=row["kind"],
            path=row["path"],
            sha256=row["sha256"],
            bytes=row["bytes"],
            produced_by=row["produced_by"],
            review_state=row["review_state"],
            created_at=row["created_at"],
        )

    def _artifact_row(self, conn: sqlite3.Connection, artifact_id: int) -> Artifact:
        row = conn.execute(
            "SELECT * FROM artifact WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self._artifact(row)

    @staticmethod
    def _archive(row: sqlite3.Row) -> Archive:
        return Archive(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            project_id=int(row["project_id"]),
            root_path=row["root_path"],
            manifest_path=row["manifest_path"],
            manifest_sha256=row["manifest_sha256"],
            created_at=row["created_at"],
        )

    def _archive_row(self, conn: sqlite3.Connection, archive_id: int) -> Archive:
        row = conn.execute(
            "SELECT * FROM archive WHERE id = ?", (archive_id,)
        ).fetchone()
        if row is None:
            raise KeyError(archive_id)
        return self._archive(row)


__all__ = ["SCHEMA_VERSION", "Registry"]
