"""Revision 0009 — one active run per meeting, enforced by the database.

Revision ID: 0009
Revises: 0008

"One run in flight per meeting" was, until this revision, only a check the code
made before it wrote: the run manager reads the meeting's active run and refuses,
and only then does the insert follow. The check stays — it refuses the common case
without writing a row — but it cannot be the rule: two clients submitting the same
meeting at the same moment both read "no active run", and a read cannot stop the
other's write, so both rows landed. The rule is therefore stated here, where every
writer must obey it, as a **unique index over the meeting's active run**.

Two things about the index's shape are deliberate:

- **It is partial.** :data:`~clear_record.service.models.RUN_STATUSES` is the
  vocabulary: ``queued`` and ``running`` are the *active* states and the other
  four — ``done``, ``failed``, ``stopped``, ``interrupted`` — are terminal. A run
  that ended holds nothing, and a meeting whose only run has finished must be
  runnable again (and again): a total ``UNIQUE (meeting_id)`` would make the
  meeting's *history* unique instead, and would refuse exactly the second run the
  product is built around. The predicate is not the only place a status is
  classified — the guard the run manager reads names the same pair — so a **new**
  status is classified in all three places: the vocabulary, the guard, and this
  ``WHERE``.
- **It is named.** ``pipeline_run_active_meeting`` is the name the mapping in
  :mod:`clear_record.service.entities` declares it under, so the parity test can
  compare the two declarations — the mapping and this revision — column for
  column and predicate for predicate.

**The index cannot be created over a registry that already holds two active runs
for one meeting, and that state is one this application produced** — it is what
two submissions racing past the check wrote. So this is the first revision that
**alters data**: before the DDL, ``_END_LOSERS`` ends every active run but one per
meeting — a ``running`` row over a ``queued`` one, otherwise the oldest — leaving
``status = 'interrupted'``, an ``ended_at`` and the reason in ``error``, which is
where the console reads an interrupted run's *why* (the restart reconciliation
writes its own reason there). ``interrupted`` is the honest state of the four:
nobody cancelled these (``stopped`` would claim the user did) and nothing ran to
an end of its own. The reason says the meeting had more than one active run and
that the registry keeps one.

What it deliberately does not do: it writes no run event (the event stream is the
run's own, and a new event kind would be a new kind for a one-off repair), and it
writes no ``progress`` summary (that record's cost primitives are what the service
measures while it watches a run; a terminal run without one is one the console
already renders). A registry with one active run per meeting — every registry that
never raced, a fresh one included — is left exactly as it is: the statement
matches nothing there, and no other column is touched at all.
"""

from __future__ import annotations

import datetime as _dt

from alembic import op
from sqlalchemy import text

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

#: The reason ``_END_LOSERS`` records on a run it ends. The user reads it on the
#: run's own row, so it says what happened rather than naming the revision, and it
#: is pinned by the test that opens such a registry.
RECONCILED_REASON = (
    "this meeting had more than one active run, and one run per meeting is what "
    "the registry keeps"
)

#: This revision's DDL: the mapping's own index, stated in the schema's history.
_DDL: tuple[str, ...] = (
    """CREATE UNIQUE INDEX IF NOT EXISTS pipeline_run_active_meeting
    ON pipeline_run (meeting_id) WHERE status IN ('queued', 'running');""",
)

#: End every active run but each meeting's keeper, in one statement.
#:
#: The keeper is the meeting's ``running`` row if it has one — a queued run
#: waiting behind a running one is the duplicate — otherwise its oldest. The
#: correlated subquery picks it with the same ordering, and every *other* active
#: row matches, which is also what makes a registry with no duplicates a no-op:
#: there the subquery returns the row's own id and nothing is updated. One
#: statement rather than a read and a loop because this step must render in
#: offline (``--sql``) mode too, where there are no rows to iterate — and because
#: a set decided in the database cannot disagree with itself between two
#: statements.
#:
#: ``owner`` and ``heartbeat_at`` are left as they are: they are the history of a
#: run that had an owner, exactly as the restart reconciliation leaves them.
_END_LOSERS = text(
    """UPDATE pipeline_run
    SET status = 'interrupted', ended_at = :ended_at, error = :reason
    WHERE status IN ('queued', 'running')
      AND id NOT IN (
          SELECT keeper.id FROM pipeline_run AS keeper
          WHERE keeper.meeting_id = pipeline_run.meeting_id
            AND keeper.status IN ('queued', 'running')
          ORDER BY (keeper.status = 'running') DESC, keeper.id
          LIMIT 1
      )"""
)


def upgrade() -> None:
    """Reconcile what the rule cannot hold, then apply this revision's DDL."""
    op.execute(
        _END_LOSERS.bindparams(
            ended_at=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            reason=RECONCILED_REASON,
        )
    )
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
