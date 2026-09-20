"""Revision 0004 — meeting notes (the ladder's v4).

Revision ID: 0004
Revises: 0003

The story: meetings gain a free-text ``notes`` column so the user's
narrative (and an agent's draft) has a durable home beside the glossary.
Project notes predate this step.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

#: This revision's step, as the retired ladder wrote it: one column added to
#: ``meeting``, as ``(table, column, DDL)``.
#:
#: SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the column is added only when
#: the table does not already carry it. That is what makes the chain
#: **re-runnable**: an open killed part-way through it replays from the base
#: (``store._pending_stamp``), and this step is the first the ladder could not
#: survive twice — its second run failed with ``duplicate column name: notes``,
#: which no later open could get past. The DDL is unchanged; only the decision to
#: run it is conditional on the shape.
_ADD: tuple[tuple[str, str, str], ...] = (
    (
        "meeting",
        "notes",
        """ALTER TABLE meeting ADD COLUMN notes TEXT NOT NULL DEFAULT '';""",
    ),
)


def upgrade() -> None:
    """Apply this revision's step, adding the column only when it is absent."""
    for table, column, ddl in _ADD:
        present = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}
        if column not in present:
            op.execute(ddl)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
