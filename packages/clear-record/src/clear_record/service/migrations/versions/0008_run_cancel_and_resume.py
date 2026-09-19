"""Revision 0008 — cancel and resume (RUN-04) (the ladder's v8).

Revision ID: 0008
Revises: 0007

RUN-04: cancel and resume. ``resumes_run_id`` links a run to the run it
continues (the chunk cache is what actually makes it cheaper; this is what
makes the link visible). ``cancel_requested_at`` is a *request* on a
``running`` run: only its owner may end it, so the column records who asked
and when, and the owner's next look at the row turns it into the terminal
``stopped``. Both are NULL for a run that neither resumes nor was asked to
stop.
"""

from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

#: This revision's DDL, the retired ladder's step as written.
_DDL: tuple[str, ...] = (
    """ALTER TABLE pipeline_run ADD COLUMN resumes_run_id INTEGER;""",
    """ALTER TABLE pipeline_run ADD COLUMN cancel_requested_at TEXT;""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the retired ladder's step as written."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
