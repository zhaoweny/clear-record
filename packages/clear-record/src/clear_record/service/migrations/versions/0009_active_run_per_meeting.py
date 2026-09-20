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
  vocabulary and :data:`~clear_record.service.models.ACTIVE_RUN_STATUSES` is the
  pair that is *active* — ``queued`` and ``running``; the other four (``done``,
  ``failed``, ``stopped``, ``interrupted``) are terminal and derive their own
  tuple from the same vocabulary. A run that ended holds nothing, and a meeting
  whose only run has finished must be runnable again (and again): a total
  ``UNIQUE (meeting_id)`` would make the meeting's *history* unique instead, and
  would refuse exactly the second run the product is built around. The pair is
  named wherever a status is classified — the guard the run manager reads
  (``store.active_run_for_meeting``), the reconciliation's compare-and-set
  (``store.interrupt_run``), the claim (``store.claim_run``), the run manager's
  own transitions, the console's run views, and the mapping's
  ``sqlite_where`` (``entities.PipelineRun``) — all of them read
  ``ACTIVE_RUN_STATUSES``. The two places that must spell it out again are this
  ``WHERE`` and :data:`_END_LOSERS`, because a revision states its own DDL rather
  than importing the application; ``tests/service/test_store.py`` compares this
  predicate with the declaration, so the copies cannot drift apart.
- **It is named.** ``pipeline_run_active_meeting`` is the name the mapping in
  :mod:`clear_record.service.entities` declares it under, so the parity test can
  compare the two declarations — the mapping and this revision — column for
  column and predicate for predicate.

**The index cannot be created over a registry that already holds two active runs
for one meeting, and that state is one this application produced** — it is what
two submissions racing past the check wrote. So this is the first revision that
**alters data**: before the DDL, :data:`_END_LOSERS` ends every active run but one
per meeting — see the keeper rule below — leaving ``status = 'interrupted'``, an
``ended_at`` and the reason in ``error``, which is where the console reads an
interrupted run's *why* (the restart reconciliation writes its own reason there).
``interrupted`` is the honest state of the four: nobody cancelled these
(``stopped`` would claim the user did) and nothing ran to an end of its own. The
reason names the run that was kept, so the console can say which submission
survived.

**The keeper is the run the service would still call live.** A ``running`` row is
preferred **only when it names an owner** — the same evidence ``runs.py``'s
``_is_dead`` reads first, as far as one statement can read it: SQL cannot probe
whether the owner's process still exists, so a ``running`` row with no owner at
all (every run the retired ladder wrote, which had no such column) is one nothing
can be executing, while one that names an owner is kept even if its heartbeat has
gone quiet — fail closed, exactly as the service does for a pid that still
exists. Among the rest the **newest** run wins, by ``created_at`` then ``id``: for
a user who resubmitted because the first looked stuck, the submission that
survives is the one they just made, not the stale one.

Where the rule keeps a dead owner's ``running`` row, the service's own
reconciliation ends it at the next start and the meeting is free again; what the
migration must not do is interrupt a run that is really executing, because that
frees the meeting and admits a second pipeline beside it. That asymmetry is why
the evidence stops at "names an owner".

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

from alembic import context, op
from sqlalchemy import text

from clear_record.core.i18n import deferred, tr

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

#: The index this revision creates, by name.
INDEX_NAME = "pipeline_run_active_meeting"

#: The reason :data:`_END_LOSERS` records on a run it ends — **translated where it
#: is written**, because the console renders the run's own ``error`` column
#: verbatim (``web/templates/_run.html``, ``_activity_run.html``), which
#: ``docs/i18n.md`` puts on the translated side of the boundary.
#:
#: ``deferred`` is the message-ID marker (the lookup happens here, at migration
#: time, not at import): :func:`upgrade` resolves it with ``tr`` and the
#: statement fills ``{run_id}`` with the run it kept — so the user reads which
#: submission survived, in their language. Placeholders are named, as the i18n
#: rule requires.
RECONCILED_REASON = deferred(
    "this meeting had more than one active run, and one run per meeting is what "
    "the registry keeps; run {run_id} is the one that survives"
)

#: This revision's DDL: the index the mapping also declares, stated here as the
#: schema's own DDL.
#:
#: No ``IF NOT EXISTS``: an object already carrying this name would make the
#: whole revision optional — the reconciliation would run and the *look-alike*
#: would be accepted, leaving the one-active-run rule unenforced while the
#: mapping and the parity test still declared it. :func:`upgrade` therefore takes
#: the two cases apart: absent, and it creates this; present and identical (a
#: re-run of a chain whose first pass was killed, see ``store._pending_stamp``),
#: and it leaves it; present and different, and it refuses the registry with the
#: object's own DDL in the message.
_DDL: tuple[str, ...] = (
    """CREATE UNIQUE INDEX pipeline_run_active_meeting
    ON pipeline_run (meeting_id) WHERE status IN ('queued', 'running');""",
)

#: End every active run but each meeting's keeper, in one statement.
#:
#: The keeper is the meeting's ``running`` row that names an owner, else its
#: newest run (``created_at`` then ``id``); the ordering states that once, in
#: ``keeper``, and both the ``NOT IN`` and the reason read it. One statement
#: rather than a read and a loop because this step must render in offline
#: (``--sql``) mode too, where there are no rows to iterate — and because a set
#: decided in the database cannot disagree with itself between two statements.
#:
#: ``error`` is the translated frame with ``{run_id}`` filled by the keeper's id
#: (SQL's ``replace`` is the only substitution available inside one statement, and
#: the placeholder is the one ``tr``/``deferred`` use). ``owner`` and
#: ``heartbeat_at`` are left as they are: they are the history of a run that had
#: an owner, exactly as the restart reconciliation leaves them.
_END_LOSERS = text(
    """WITH active AS (
    SELECT id, meeting_id, status, owner, created_at FROM pipeline_run
    WHERE status IN ('queued', 'running')
),
keeper AS (
    SELECT active.id, active.meeting_id FROM active
    WHERE active.id = (
        SELECT pick.id FROM active AS pick
        WHERE pick.meeting_id = active.meeting_id
        ORDER BY (pick.status = 'running' AND pick.owner IS NOT NULL) DESC,
                 pick.created_at DESC, pick.id DESC
        LIMIT 1
    )
)
UPDATE pipeline_run
SET status = 'interrupted',
    ended_at = :ended_at,
    error = replace(
        :reason,
        '{run_id}',
        CAST((
            SELECT keeper.id FROM keeper
            WHERE keeper.meeting_id = pipeline_run.meeting_id
        ) AS TEXT)
    )
WHERE status IN ('queued', 'running')
  AND id NOT IN (SELECT keeper.id FROM keeper)"""
)


def _normalized(sql: str) -> str:
    """One DDL statement as text that two spellings of it can be compared by.

    SQLite stores the text it was given, minus ``IF NOT EXISTS`` (it drops that
    clause itself), so whitespace and the clause are the only things to level.
    """
    return " ".join(sql.replace("IF NOT EXISTS", "").split()).rstrip(";")


def _existing_index() -> str | None:
    """The DDL of the index already carrying this revision's name, if any.

    ``None`` when the name is free. Offline rendering cannot look, and answers
    ``None`` — it has no registry to inspect and the path that checks is the
    online one.
    """
    if context.is_offline_mode():
        return None
    row = (
        op.get_bind()
        .execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": INDEX_NAME},
        )
        .first()
    )
    return None if row is None else row[0]


def _create_the_index() -> None:
    """Create the index, or refuse a registry that carries another one by that name."""
    existing = _existing_index()
    if existing is None:
        for statement in _DDL:
            op.execute(statement)
        return
    if _normalized(existing) == _normalized(_DDL[0]):
        # The chain was re-run (an open killed part-way through it replays from
        # the base): this is this revision's own index, already stated.
        return
    raise RuntimeError(
        f"the registry carries an index named {INDEX_NAME} that revision 0009 did "
        f"not create and does not declare: {existing}. The one-active-run rule "
        f"cannot be stated over it — drop that object and open the registry again."
    )


def upgrade() -> None:
    """Reconcile what the rule cannot hold, then state the rule."""
    op.execute(
        _END_LOSERS.bindparams(
            ended_at=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            reason=tr(RECONCILED_REASON),
        )
    )
    _create_the_index()


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
