"""Revision 0007 — run ownership (RUN-02) (the ladder's v7).

Revision ID: 0007
Revises: 0006

RUN-02: who a run belongs to. ``origin`` is the surface that started it;
``owner`` the process that claimed it (the conditional transition that makes
the queue cross-process); ``heartbeat_at`` the owner's last proof of life,
refreshed while it executes. All three are NULL for a run recorded before
this revision — read as unknown, never guessed.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """ALTER TABLE pipeline_run ADD COLUMN origin TEXT;""",
    """ALTER TABLE pipeline_run ADD COLUMN owner TEXT;""",
    """ALTER TABLE pipeline_run ADD COLUMN heartbeat_at TEXT;""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
