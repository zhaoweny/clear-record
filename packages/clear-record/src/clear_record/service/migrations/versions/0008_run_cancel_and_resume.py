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

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

#: This revision's step, as the retired ladder wrote it: two columns added to
#: ``pipeline_run``, as ``(table, column, DDL)``.
#:
#: SQLite has no ``ADD COLUMN IF NOT EXISTS``, so each column is added only when
#: the table does not already carry it — which is what lets the chain be re-run
#: after an open was killed part-way through it (see ``store._pending_stamp``).
#: The DDL is unchanged, and ``resumes_run_id`` stays a plain column: its
#: reference is checked by the store, not by a foreign key.
_ADD: tuple[tuple[str, str, str], ...] = (
    (
        "pipeline_run",
        "resumes_run_id",
        """ALTER TABLE pipeline_run ADD COLUMN resumes_run_id INTEGER;""",
    ),
    (
        "pipeline_run",
        "cancel_requested_at",
        """ALTER TABLE pipeline_run ADD COLUMN cancel_requested_at TEXT;""",
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
