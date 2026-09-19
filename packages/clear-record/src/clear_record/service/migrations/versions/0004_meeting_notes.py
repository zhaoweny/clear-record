"""Revision 0004 — meeting notes (the ladder's v4).

Revision ID: 0004
Revises: 0003

The story: meetings gain a free-text ``notes`` column so the user's
narrative (and an agent's draft) has a durable home beside the glossary.
Project notes predate this step.
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """ALTER TABLE meeting ADD COLUMN notes TEXT NOT NULL DEFAULT '';""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
