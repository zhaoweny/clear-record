"""Revision 0003 — the archive spine (the ladder's v3).

Revision ID: 0003
Revises: 0002

The archive spine: each immutable, checksummed copy of a meeting's tapes and
record. The files live in the user's archive root; the registry stores the
copy's paths and the manifest's checksum (ADR-0006/ADR-0007).
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at      TEXT NOT NULL
);""",
    """CREATE INDEX IF NOT EXISTS archive_meeting ON archive (meeting_id);""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
