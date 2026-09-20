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

- **It is partial.** :data:`~clear_record.service.lifecycle.RUN_STATUSES` is the
  vocabulary and :data:`~clear_record.service.lifecycle.ACTIVE_RUN_STATUSES` is
  the pair that is *active* — ``queued`` and ``running``; the other four
  (``done``, ``failed``, ``stopped``, ``interrupted``) are terminal and derive
  their own tuple from the same vocabulary. A run that ended holds nothing, and a meeting
  whose only run has finished must be runnable again (and again): a total
  ``UNIQUE (meeting_id)`` would make the meeting's *history* unique instead, and
  would refuse exactly the second run the product is built around. The pair is
  named wherever a status is classified — the guard the run manager reads
  (``store.active_run_for_meeting``), the run manager's own transitions, the
  console's run views and the mapping's ``sqlite_where``
  (``entities.PipelineRun``) — all of them read ``ACTIVE_RUN_STATUSES``. The two
  places that must spell it out again are this ``WHERE`` and :data:`_END_LOSERS`,
  because a revision states its own DDL rather than importing the application;
  ``tests/service/test_store.py`` compares this predicate with the declaration, so
  the copies cannot drift apart.

  The pair is read by the guard the run manager asks
  (``store.active_run_for_meeting``), the console's run views and the mapping's
  ``sqlite_where``; the claim's node-wide clause spells ``running`` for itself,
  and the *claim* itself (``store.claim_run``) reads the state ``lifecycle.CLAIM``
  declares, while the reconciliation's compare-and-set (``store.interrupt_run``)
  reads the state ``lifecycle.INTERRUPT`` declares — both narrower than the pair.
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
import re

from alembic import context, op
from sqlalchemy import text

from clear_record.core.i18n import deferred, tr

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

#: The index this revision creates, by name.
INDEX_NAME = "pipeline_run_active_meeting"

#: The reason :data:`_END_LOSERS` records on a run it ends — **rendered where it
#: is written**, which is the one exception to how that column is otherwise
#: filled. :data:`~clear_record.service.lifecycle.RESTART_REASON` is stored as its
#: message *ID*, which the console renders where it shows the column
#: (``web/templates/_run.html``, ``_activity_run.html``); this frame cannot be,
#: because of its placeholder: ``{run_id}`` names the run that survived, and the
#: only statement that knows which one that is is this revision's own, which fills
#: it as it writes (SQL's ``replace``). Stored unfilled it would reach the console
#: as a literal ``{run_id}``, so the sentence is looked up here instead, and a run
#: this revision ends carries the migrating process's locale. ``docs/i18n.md``
#: names those as the column's only two locale-dependent cases.
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
#: the two cases apart: absent, and it creates this; present and *this revision's
#: own* — the same name, column, uniqueness and partiality, and a predicate that
#: is this rule with an equal or **wider** state list (see :data:`_INDEX_SHAPE`
#: and :func:`_masked_states`) — and it leaves it; present and anything else,
#: including a predicate narrowed to one state or one that is a different rule
#: altogether, and it refuses the registry with the object's own DDL in the
#: message.
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


#: This revision's own index, as SQLite describes it: unique, partial, and the
#: columns it covers in order. :func:`_create_the_index` reads what it finds with
#: :func:`_index_shape` and compares it with this.
#:
#: Identity is only **half** of the check, and deliberately so: an index sharing
#: name, column, uniqueness and partiality with a *different* predicate — one
#: narrowed to a single state, say — enforces a different rule and must not pass
#: as this revision's own. The other half is the predicate itself, compared by
#: :func:`_masked_states`.
#:
#: Why not by DDL text alone, which is what this revision did first: the one path
#: that meets an index it did not just create is the replay-from-base repair
#: (:func:`_pending_stamp` in ``store``), which runs every revision again over a
#: schema that is already built — and a successor revision may legitimately have
#: **widened** this index, because adding an active state to
#: ``ACTIVE_RUN_STATUSES`` requires a revision, which restates the predicate with
#: the new state in it. That widened index is this revision's own, and comparing
#: text refused the very registry the repair exists for.
_INDEX_SHAPE = (True, True, ("meeting_id",))

#: The ``status IN (...)`` list a statement states, and its contents.
_STATES = re.compile(r"status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


def _declared_states(sql: str) -> frozenset[str] | None:
    """The states a statement's ``status IN (...)`` accepts, or ``None`` if it has none."""
    match = _STATES.search(sql)
    if match is None:
        return None
    return frozenset(part.strip().strip("'\"") for part in match.group(1).split(","))


def _masked_states(sql: str) -> str:
    """One DDL statement with its state list masked, whitespace levelled.

    SQLite stores the text it was given, minus ``IF NOT EXISTS`` (it drops that
    clause itself), so whitespace and the clause are the only things to level —
    and the state list is *masked* rather than compared, because that is the one
    part a successor revision may legitimately widen. What is left is compared for
    equality: a predicate that reads any other way is a different rule.
    """
    masked = _STATES.sub("status IN (…)", sql.replace("IF NOT EXISTS", ""))
    return " ".join(masked.split()).rstrip(";")


#: The states this revision's own DDL accepts (``ACTIVE_RUN_STATUSES`` at the time
#: it was written, spelled out because a revision states its own DDL).
_INDEX_STATES: frozenset[str] = _declared_states(_DDL[0]) or frozenset()


def _index_shape() -> tuple[bool, bool, tuple[str, ...]] | None:
    """How the index already carrying this revision's name is built, if any.

    SQLite's own description of it — ``unique``, ``partial`` and the indexed
    columns, in order — rather than its DDL text (see :data:`_INDEX_SHAPE`).
    ``None`` when the name is free. Offline rendering cannot look, and answers
    ``None``: it has no registry to inspect, and the path that checks is the
    online one.
    """
    if context.is_offline_mode():
        return None
    bind = op.get_bind()
    named = [
        row
        for row in bind.execute(
            text('SELECT name, "unique", partial FROM pragma_index_list(:table)'),
            {"table": "pipeline_run"},
        )
        if row[0] == INDEX_NAME
    ]
    if not named:
        return None
    _, unique, partial = named[0]
    columns = tuple(
        row[0]
        for row in bind.execute(
            text("SELECT name FROM pragma_index_info(:index) ORDER BY seqno"),
            {"index": INDEX_NAME},
        )
    )
    return (bool(unique), bool(partial), columns)


def _index_ddl() -> str:
    """The DDL of the index carrying this revision's name, for the refusal."""
    row = (
        op.get_bind()
        .execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": INDEX_NAME},
        )
        .first()
    )
    return "" if row is None else row[0]


def _create_the_index() -> None:
    """Create the index, or refuse a registry that carries another one by that name."""
    found = _index_shape()
    if found is None:
        for statement in _DDL:
            op.execute(statement)
        return
    if found == _INDEX_SHAPE:
        existing = _index_ddl()
        states = _declared_states(existing)
        # This revision's own index, already stated. Two ways it arrives here: the
        # chain was re-run (an open killed part-way through it replays from the
        # base), or a successor revision widened the predicate and the replay is
        # looking at the wider index. Both pass because the state list is read as
        # a **superset** — every state this revision's DDL names is in it — while
        # every other part of the statement must still be this revision's own, so
        # a predicate narrowed to one state, or one that is a different rule, falls
        # through to the refusal below.
        if (
            states is not None
            and _INDEX_STATES <= states
            and _masked_states(existing) == _masked_states(_DDL[0])
        ):
            return
    raise RuntimeError(
        tr(
            "the registry carries an index named {index} that revision 0009 did not "
            "create and does not declare: {ddl}. The one-active-run rule cannot be "
            "stated over it — drop that object and open the registry again.",
            index=INDEX_NAME,
            ddl=_index_ddl(),
        )
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
