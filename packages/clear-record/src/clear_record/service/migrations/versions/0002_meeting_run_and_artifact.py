"""Revision 0002 — the meeting, run and artifact spine (the ladder's v2).

Revision ID: 0002
Revises: 0001

The meeting/run/artifact spine: a project's meetings, the tapes chosen for
each, the pipeline runs against them, and the artifacts they produce.
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meeting (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug           TEXT NOT NULL,
    title          TEXT NOT NULL,
    recorded_at    TEXT,
    workspace_path TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    created_at     TEXT NOT NULL,
    UNIQUE (project_id, slug)
);""",
    """CREATE TABLE IF NOT EXISTS recording_set (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    paths      TEXT NOT NULL,
    created_at TEXT NOT NULL
);""",
    """CREATE TABLE IF NOT EXISTS pipeline_run (
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
);""",
    """CREATE TABLE IF NOT EXISTS artifact (
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
);""",
    """CREATE INDEX IF NOT EXISTS meeting_project ON meeting (project_id);""",
    """CREATE INDEX IF NOT EXISTS pipeline_run_meeting ON pipeline_run (meeting_id);""",
    """CREATE INDEX IF NOT EXISTS artifact_meeting ON artifact (meeting_id);""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
