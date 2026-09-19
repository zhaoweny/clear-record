"""Revision 0006 — durable run state and the node queue (the ladder's v6).

Revision ID: 0006
Revises: 0005

Durable run state and the node queue: the live event stream is persisted per
run (so the run view replays after a restart), and a run records the
resolved options it will execute with (so a queued run survives a restart
and is picked back up). Runs are ordered by id as the node's FIFO.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS run_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);""",
    """CREATE INDEX IF NOT EXISTS run_event_run ON run_event (run_id);""",
    """ALTER TABLE pipeline_run ADD COLUMN run_options TEXT;""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
