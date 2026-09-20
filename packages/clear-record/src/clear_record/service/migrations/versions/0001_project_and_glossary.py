"""Revision 0001 — the project and glossary baseline (the ladder's v1).

Revision ID: 0001
Revises: nothing (an empty registry)

The history opens at the ladder's first step: the app-owned project and
glossary tables. Revisions 0002–0009 carry a registry from there to today's
schema, and the *adoption* of the ladder's steps adds no table, column or index:
what they describe is the schema the registry already has (0009 alone alters
data, and only rows the product itself raced into — see that revision). The one statement of
the ladder's step that has no counterpart here is its own ``schema_version``
table: the version state is ``alembic_version`` from this revision on, and
an existing registry's ``schema_version`` is read once, when it is adopted.
"""

from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS project (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    notes                TEXT NOT NULL DEFAULT '',
    default_archive_root TEXT,
    created_at           TEXT NOT NULL
);""",
    """CREATE TABLE IF NOT EXISTS glossary_term (
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
);""",
    """CREATE INDEX IF NOT EXISTS glossary_term_project ON glossary_term (project_id);""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the ladder's first step, less its version table."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
