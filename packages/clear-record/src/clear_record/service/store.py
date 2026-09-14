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
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clear_record.service.models import (
    TERM_AUTHORS,
    TERM_STATUSES,
    GlossaryTerm,
    Project,
)
from clear_record.service.paths import registry_path

SCHEMA_VERSION = 1

_SCHEMA = """
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
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            return
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        version = int(row["version"]) if row else 0
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"registry schema version {version} != supported {SCHEMA_VERSION}; "
                "this database was written by a different clear-record version"
            )

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


__all__ = ["SCHEMA_VERSION", "Registry"]
