"""The SQLite registry: projects and their glossary terms.

One database (stdlib :mod:`sqlite3`, no dependency) holds the **app-owned**
project/glossary state under the XDG data directory. Audio, workspaces and
archives stay as files elsewhere; the registry stores metadata only
(ADR-0007/ADR-0013).

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

import datetime as _dt
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clear_record.service.models import (
    MEETING_STATUSES,
    RUN_STATUSES,
    TERM_AUTHORS,
    TERM_STATUSES,
    Artifact,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
)
from clear_record.service.paths import registry_path

SCHEMA_VERSION = 2

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

# Forward-only: each entry is (version it produces, DDL). A fresh registry runs
# them all; an existing one runs only those newer than its stored version.
_MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1), (2, _SCHEMA_V2))


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


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
        with self._connect() as conn:
            slug = (slug or "").strip() or self._unique_slug(conn, name)
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
        with self._connect() as conn:
            slug = (slug or "").strip() or self._unique_meeting_slug(
                conn, project.id, title
            )
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
            cur = conn.execute(
                "INSERT INTO recording_set (meeting_id, paths, created_at) VALUES (?, ?, ?)",
                (meeting_id, json.dumps(clean), _now()),
            )
            return self._recording_set_row(conn, cur.lastrowid)

    def latest_recording_set(self, meeting_id: int) -> RecordingSet | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recording_set WHERE meeting_id = ? ORDER BY id DESC LIMIT 1",
                (meeting_id,),
            ).fetchone()
        return self._recording_set(row) if row else None

    # --- pipeline runs ----------------------------------------------------- #
    def create_run(
        self,
        meeting_id: int,
        *,
        backend: str | None = None,
        model: str | None = None,
        language: str | None = None,
        options: dict | None = None,
    ) -> PipelineRun:
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pipeline_run"
                " (meeting_id, status, backend, model, language, options, created_at)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    backend,
                    model,
                    language,
                    json.dumps(options) if options else None,
                    _now(),
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
    def _run(row: sqlite3.Row) -> PipelineRun:
        return PipelineRun(
            id=int(row["id"]),
            meeting_id=int(row["meeting_id"]),
            status=row["status"],
            backend=row["backend"],
            model=row["model"],
            language=row["language"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            error=row["error"],
            created_at=row["created_at"],
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


__all__ = ["SCHEMA_VERSION", "Registry"]
