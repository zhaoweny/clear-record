"""Revision 0007 — run ownership (the ladder's v7).

Revision ID: 0007
Revises: 0006

Who a run belongs to: ``origin`` is the surface that started it;
``owner`` the process that claimed it (the conditional transition that makes
the queue cross-process); ``heartbeat_at`` the owner's last proof of life,
refreshed while it executes. All three are NULL for a run recorded before
this revision — read as unknown, never guessed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

#: This revision's step, as the retired ladder wrote it: three columns added to
#: ``pipeline_run``, as ``(table, column, DDL)``.
#:
#: SQLite has no ``ADD COLUMN IF NOT EXISTS``, so each column is added only when
#: the table does not already carry it — which is what lets the chain be re-run
#: after an open was killed part-way through it (see ``store._pending_stamp``).
#: The DDL is unchanged; only running it is conditional on the shape.
_ADD: tuple[tuple[str, str, str], ...] = (
    ("pipeline_run", "origin", """ALTER TABLE pipeline_run ADD COLUMN origin TEXT;"""),
    ("pipeline_run", "owner", """ALTER TABLE pipeline_run ADD COLUMN owner TEXT;"""),
    (
        "pipeline_run",
        "heartbeat_at",
        """ALTER TABLE pipeline_run ADD COLUMN heartbeat_at TEXT;""",
    ),
)


def upgrade() -> None:
    """Apply this revision's step, adding each column only when it is absent."""
    for table, column, ddl in _ADD:
        present = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}
        if column not in present:
            op.execute(ddl)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
