"""Revision 0006 — durable run state and the node queue (the ladder's v6).

Revision ID: 0006
Revises: 0005

Durable run state and the node queue: the live event stream is persisted per
run (so the run view replays after a restart), and a run records the
resolved options it will execute with (so a queued run survives a restart
and is picked back up). Runs are ordered by id as the node's FIFO.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written: the statements that
#: are their own guard.
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
)

#: The step's one statement that is not its own guard: one column added to
#: ``pipeline_run``, as ``(table, column, DDL)``. SQLite has no ``ADD COLUMN IF
#: NOT EXISTS``, so it is applied only when the column is missing — which is what
#: lets the chain be re-run after an open was killed part-way through it (see
#: ``store._pending_stamp``). The DDL is unchanged; only running it is
#: conditional on the shape.
_ADD: tuple[tuple[str, str, str], ...] = (
    (
        "pipeline_run",
        "run_options",
        """ALTER TABLE pipeline_run ADD COLUMN run_options TEXT;""",
    ),
)


def upgrade() -> None:
    """Apply this revision's DDL, adding the column only when it is absent."""
    for statement in _DDL:
        op.execute(statement)
    for table, column, ddl in _ADD:
        present = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}
        if column not in present:
            op.execute(ddl)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
