"""Revision 0005 — the managed workspace's tape (the ladder's v5).

Revision ID: 0005
Revises: 0004

The managed workspace (ADR-0024): a tape uploaded to the node is recorded
with the copy's integrity facts (``sha256`` and ``bytes``), which the
path-only recording set cannot carry. The tape's path is also added to the
meeting's recording set, so the pipeline (runs/archive) reads an upload
exactly like a user-typed path.
"""

from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS tape (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);""",
    """CREATE INDEX IF NOT EXISTS tape_meeting ON tape (meeting_id);""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
