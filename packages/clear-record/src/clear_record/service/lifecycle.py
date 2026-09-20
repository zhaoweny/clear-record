"""A pipeline run's lifecycle: its states, the moves between them, and what each
move announces.

A run's state is written in the registry, performed by the run manager, shown by
the console and reached by the agent tools, so "what a run state is" was once
said in five places: the vocabulary, a hand-copied terminal tuple beside it, the
manager's literal comparisons, the queries in
:mod:`~clear_record.service.store`, and the partial index the mapping declares
and revision ``0009`` creates. A sixth state had to be classified in every one of
them, and the site that was missed failed in silence — a status no query lists is
a run nothing sees.

This module is that classification, once:

* the **states** (:data:`RUN_STATUSES`), and which of them are in flight
  (:data:`ACTIVE_RUN_STATUSES`), terminal (:data:`TERMINAL_STATUSES`), resumable
  (:data:`RESUMABLE_STATUSES`) or bad news nobody asked for
  (:data:`ATTENTION_STATUSES`) — the middle two sets computed from the vocabulary,
  the first and last declared pairs;
* the **moves** between the states (:class:`RunTransition`, :data:`TRANSITIONS`):
  the state each starts from, the state it lands in, who may ask for it, and the
  notification it raises;
* the **one-active-run rule** as a named operation
  (:func:`active_run_predicate`), which the mapping's ``sqlite_where`` renders
  from :data:`ACTIVE_RUN_STATUSES` — the same pair the registry's guard reads;
* the two **sentences** a reader is shown for a move: the refusal a second
  submission gets (:data:`RUN_IN_FLIGHT`) and the reason a run is interrupted
  (:data:`RESTART_REASON`). A *third* reason is written to the same column and
  deliberately stays elsewhere: ``RECONCILED_REASON`` (revision ``0009``) is what
  the migration writes on the runs it ends, and it lives with the revision
  because the migration translates it as it writes.

What it deliberately is not: the **manager**. Claiming, draining, threading,
beating and reaping stay in :mod:`~clear_record.service.runs`, and the
conditional statements that perform a move stay in
:mod:`~clear_record.service.store` — this module says what the moves are, not
when to make one. Nor is it the diagnostic log: the JSONL records the manager
writes (``run.enqueued``, ``run.stopped``, ``run.reconciled``, …) are that
surface's vocabulary and are not derived from these names.

**Adding a state is one edit here.** What follows the edit, and what does not, is
worth naming:

* :data:`TERMINAL_STATUSES` and :data:`RESUMABLE_STATUSES` are **computed** from
  the vocabulary, so a new state is classified by landing in it;
* :data:`ACTIVE_RUN_STATUSES` and :data:`ATTENTION_STATUSES` are **declared
  pairs** — a state joins one by being named there, and those two names are the
  only places the edit has to mention the new state instead of letting it fall
  out of the vocabulary;
* the registry's validation
  (:meth:`~clear_record.service.store.Registry.update_run`) and its run queries
  (``runs_with_status``, ``active_run_for_meeting``, ``finished_runs``,
  ``oldest_queued_run``) read these declarations, so a new state is neither
  refused as unknown nor invisible to a query;
* :func:`active_run_predicate` renders the mapping's index predicate from
  :data:`ACTIVE_RUN_STATUSES`, so an active state moves the *mapping's* rule with
  it — and the database's only with a revision beside it: the index in every
  registry, whether this build created it or migrated it, is revision ``0009``'s
  literal pair, and no ``create_all`` ever emits the mapping's. The state that the
  one edit really is, is a **terminal** one — the index names no terminal state, so
  adding one cannot disagree with it;
* the **revision's** own ``WHERE`` does *not* follow — a revision states its own
  DDL, because the schema's history is frozen and a released revision cannot
  import today's application — so that copy is **pinned** instead:
  ``tests/service/test_store.py`` reads the predicate the database actually built
  and refuses the registry's open when it disagrees with
  :func:`active_run_predicate`.
"""

from __future__ import annotations

import dataclasses

from clear_record.core.i18n import deferred

# --- the states ------------------------------------------------------------- #

#: A run **waiting** for the node's queue: durable, ordered, not yet executing.
QUEUED = "queued"
#: A run the queue is **executing**, and the state its owner keeps alive with its
#: heartbeat.
RUNNING = "running"
#: The run reached its own end: its artifacts are registered and its meeting is
#: ``recorded``.
DONE = "done"
#: The run stopped on an error — the pipeline raised, the queued run could not
#: execute at all, or its stored options are ones this build refuses to read.
FAILED = "failed"
#: The run was **cancelled**. Not a failure: the user asked for it, and what the
#: run completed is in its cost record.
STOPPED = "stopped"
#: The node died while the run was live. Distinct from :data:`FAILED`: the work
#: did not necessarily fail, and the run's event stream stays readable — which is
#: what lets a resume continue it.
INTERRUPTED = "interrupted"

#: A pipeline run's lifecycle, in the order the states are reached.
RUN_STATUSES = (QUEUED, RUNNING, DONE, FAILED, STOPPED, INTERRUPTED)

#: The runs that are **in flight**: a run a meeting already has, as opposed to one
#: it had. This pair is the one-active-run rule's own vocabulary, and the readers
#: of the *rule* are the registry's guard
#: (:meth:`~clear_record.service.store.Registry.active_run_for_meeting`), the
#: claim's node-wide clause, the console's run views and the mapping's index
#: predicate (:func:`active_run_predicate`) — so "one run in flight per meeting"
#: cannot come to mean two things. The claim and the reconciliation's
#: compare-and-set read **narrower** sets than this pair: the state their own move
#: is legal from (:data:`CLAIM`, :data:`INTERRUPT`), which is what binds those two
#: statements to the declaration rather than to a copy of it.
ACTIVE_RUN_STATUSES = (QUEUED, RUNNING)

#: The runs that are **over**: the complement of :data:`ACTIVE_RUN_STATUSES`, in
#: :data:`RUN_STATUSES` order. Derived rather than restated, so a seventh state
#: is classified by landing in the vocabulary alone.
TERMINAL_STATUSES = tuple(
    status for status in RUN_STATUSES if status not in ACTIVE_RUN_STATUSES
)

#: The terminal runs a **resume** may continue: :data:`DONE` is not one of them —
#: re-running a finished meeting is the run form's job, not a resume.
RESUMABLE_STATUSES = tuple(status for status in TERMINAL_STATUSES if status != DONE)

#: The terminal runs that mean the work **did not happen and nobody asked for
#: that**: it failed, or the node died under it. :data:`DONE` (finished) and
#: :data:`STOPPED` (asked to stop) are the two outcomes that are not, so a reader
#: pointing at what went wrong — the console's status chip says "needs attention"
#: — asks this rather than listing the pair itself.
ATTENTION_STATUSES = (FAILED, INTERRUPTED)

#: The node's own **queue**. Not a surface and never recorded on a row: the actor
#: that claims a run, ends the one it executes, and reaps the one whose owner
#: died — this process acting on the registry's own state.
QUEUE = "queue"


# --- the moves -------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class RunTransition:
    """One move of a run: where it starts, where it lands, who may ask, and
    whether a receiver hears about it.

    ``sources`` is empty for the one move that **creates** a run — there is no
    state to leave — and holds the states the move is legal from otherwise. A
    **conditional** statement reads it as its own predicate: ``claim_run`` moves a
    row :data:`CLAIM` declares, ``stop_run`` one :data:`STOP_QUEUED` declares,
    ``interrupt_run`` one :data:`INTERRUPT` declares, ``fail_unreadable_run`` one
    :data:`FAIL` declares. So no conditional statement can perform a move the
    lifecycle does not declare, and an undeclared move has no such statement: the
    SQL cannot drift from the declaration.

    The two ``sources`` that are **declaration rather than predicate** are
    :data:`FINISH`'s and :data:`STOP_RUNNING`'s, and they are named here rather
    than left to be discovered. Both are performed by
    :meth:`~clear_record.service.store.Registry.update_run`, the *unconditional*
    write the run's own owner ends it with: it validates the target against the
    vocabulary and does not constrain the state it leaves, because the caller is
    the process executing that run and the row is the one it holds. Guarding that
    write with these sources would refuse a row moved out of order by something
    that is not a run at all — the end-to-end seed builds a finished run's history
    by writing ``done`` on a queued row — which is a behaviour change this module
    does not make. What those two sources describe is the move as a surface or an
    owner asks for it, and the state a reader should expect to find it in.

    ``actors`` names who may ask for the move — a surface of :data:`RUN_ORIGINS`,
    or :data:`QUEUE` — and :data:`RUN_ORIGINS` *is* :data:`ENQUEUE`'s actors: the
    origins are the surfaces that may create a run, and both start paths validate
    an origin against them. The queue's moves name :data:`QUEUE`, the manager that
    claims, ends and reaps the runs it holds; which actor performs each of them is
    pinned by ``tests/service/test_lifecycle.py``, so the field is a declaration
    the tree can be checked against rather than a comment.

    ``announced`` says whether the move is published to a webhook receiver under
    :attr:`event`. The moves that announce are the run half of the receiver's
    vocabulary (:data:`ANNOUNCEMENTS`); the others move the row and leave the log
    to say so, without a notification nothing subscribed to.

    ``name`` is the outcome's own word — what the move is known by, and what
    :attr:`event` is derived from — not the state it lands in: the move into
    ``done`` is *finished*, and a receiver subscription says so.
    """

    name: str
    sources: tuple[str, ...]
    target: str
    actors: tuple[str, ...]
    announced: bool = False

    @property
    def event(self) -> str:
        """The notification this move raises, ``run.<name>``.

        Derived from the move's own name — the word the outcome is known by —
        rather than written beside it, so declaring a move declares its
        notification: there is no second place that can name it and no way to
        name a move without one. A move that announces is published under this
        name, and the name reaches the receiver's vocabulary through
        :data:`ANNOUNCEMENTS`.
        """
        return f"run.{self.name}"


#: **Enqueue**: a surface creates the run in ``queued``, at the back of the node's
#: FIFO. The move :meth:`~clear_record.service.runs.RunManager.start` makes, and
#: the one place a run's origin is recorded.
ENQUEUE = RunTransition("enqueued", (), QUEUED, ("console", "api", "mcp", "cli"))

#: Which surface **started** a run. ``console`` is the local console UI,
#: ``api`` the HTTP JSON API (a script or an integration), ``mcp`` the stdio MCP
#: server an agent harness drives, ``cli`` the command line. It is recorded when
#: the run is enqueued, so a run's provenance survives the restart that ends its
#: process — and it is declared as the actors of the move that creates a run
#: rather than beside it, because the origins *are* the surfaces that may enqueue
#: one.
RUN_ORIGINS: tuple[str, ...] = ENQUEUE.actors

#: **Start**: the queue claims the head of the FIFO, and the pipeline executes.
#: One conditional update decides it
#: (:meth:`~clear_record.service.store.Registry.claim_run`), so two claimants
#: cannot both write the row and exactly one of them executes the run.
CLAIM = RunTransition("started", (QUEUED,), RUNNING, (QUEUE,), announced=True)

#: **Finish**: the pipeline returned and its artifacts are registered. The
#: terminal outcome a resume starts from.
FINISH = RunTransition("finished", (RUNNING,), DONE, (QUEUE,), announced=True)

#: **Fail**: the pipeline raised, the queued run cannot execute at all (no
#: workspace, no tape set), or its stored options are unreadable to this build.
#: Legal from every **in-flight** state — a run that has not ended is the run that
#: can fail, which is what :data:`ACTIVE_RUN_STATUSES` means — declared from that
#: pair rather than written out, so a state that becomes in flight is failable.
FAIL = RunTransition("failed", ACTIVE_RUN_STATUSES, FAILED, (QUEUE,), announced=True)

#: **Stop** a run **before anyone claimed it**: nothing is executing it, so the
#: cancel *is* the terminal transition. The drain never picks the row up and a
#: restart has nothing to resurrect.
STOP_QUEUED = RunTransition("stopped", (QUEUED,), STOPPED, RUN_ORIGINS)

#: **Stop** a run **its owner is executing**: the requester records the request
#: and the owner ends the run at its next safe boundary, because a row that keeps
#: executing must not read as stopped.
STOP_RUNNING = RunTransition("stopped", (RUNNING,), STOPPED, (QUEUE,))

#: **Interrupt**: no live process holds the run — its owner died, or an owner
#: this node cannot probe stopped beating. The startup reconciliation's own move.
INTERRUPT = RunTransition("interrupted", (RUNNING,), INTERRUPTED, (QUEUE,))

#: Every move a run can make, in the order a run makes it.
TRANSITIONS: tuple[RunTransition, ...] = (
    ENQUEUE,
    CLAIM,
    FINISH,
    FAIL,
    STOP_QUEUED,
    STOP_RUNNING,
    INTERRUPT,
)

#: The notifications a receiver can subscribe to: the moves that announce, in
#: declared order, under each move's own derived name. The webhook vocabulary's
#: run half is built from this
#: (:data:`~clear_record.service.webhooks.EMITTED_EVENTS`), so a move that
#: announces reaches a receiver by being declared and nothing else.
ANNOUNCEMENTS: tuple[str, ...] = tuple(
    transition.event for transition in TRANSITIONS if transition.announced
)

#: The run notifications, named where they are emitted. Each is its own move's
#: derived :attr:`~RunTransition.event`, kept as a module name because this is the
#: service's published vocabulary — not a second declaration of the string.
RUN_STARTED = CLAIM.event
RUN_FINISHED = FINISH.event
RUN_FAILED = FAIL.event


# --- the one-active-run rule ------------------------------------------------ #


def active_run_predicate(column: str = "status") -> str:
    """The one-active-run rule as SQL: ``<column> IN ('queued', 'running')``.

    The rule is the **database's** — a partial unique index over the meeting's
    active run, which revision ``0009`` creates — so the second of two
    submissions that race past the manager's read cannot be written at all.
    SQLAlchemy wants that predicate as literal text for ``sqlite_where``, which is
    why the rule is rendered here, from :data:`ACTIVE_RUN_STATUSES`, rather than
    spelled beside the index: :class:`~clear_record.service.entities.PipelineRun`
    reads the result. What a second **active** state needs is therefore one edit
    *plus a revision*: the index every registry carries is revision ``0009``'s
    literal pair, and the mapping's DDL is never emitted
    (``migrations/env.py`` sets ``target_metadata=None``, and nothing calls
    ``create_all``). The one edit the rule is about is a **terminal** state: the
    index names no terminal state, so adding one cannot leave the database behind.
    """
    rendered = ", ".join(f"'{status}'" for status in ACTIVE_RUN_STATUSES)
    return f"{column} IN ({rendered})"


# --- the sentences a reader is shown ---------------------------------------- #


#: The sentence a second submission for one meeting is refused with — the one
#: message the manager's guard, the database's index-based refusal and the JSON
#: API's own pre-check all answer with, so the same condition cannot come to read
#: two ways. A message ID, looked up where it is shown: the console's start form
#: re-renders the live run's fragment instead of printing it, and the JSON API and
#: the MCP tool carry the ID (both are machine-facing surfaces, ``docs/i18n.md``).
RUN_IN_FLIGHT = deferred("a run is already in flight for this meeting")

#: The reason startup reconciliation records on a run left ``running`` by a dead
#: process (:data:`INTERRUPT`). It is stored in the run's ``error`` column so the
#: console (and a diagnostics bundle) can show *why* the run is interrupted, not
#: just that it is.
#:
#: What is written **to the row** is this message ID, not a rendered sentence:
#: that column is machine-read — the JSON API and the MCP tool answer with it, and
#: ``docs/i18n.md`` puts a *status value* on the never-translated side — so it must
#: not hold text looked up in whatever locale the writing process happened to run
#: in. The console renders the ID where it shows the column
#: (``web/templates/_run.html``, ``_activity_run.html``), exactly as it renders the
#: progress message the run's ``progress`` summary carries unrendered; the JSONL
#: log record carries the ID unrendered too. ``deferred`` is the extraction
#: marker.
RESTART_REASON = deferred("the console restarted while this run was in flight")


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ANNOUNCEMENTS",
    "ATTENTION_STATUSES",
    "CLAIM",
    "DONE",
    "ENQUEUE",
    "FAIL",
    "FAILED",
    "FINISH",
    "INTERRUPT",
    "INTERRUPTED",
    "QUEUE",
    "QUEUED",
    "RESUMABLE_STATUSES",
    "RESTART_REASON",
    "RUN_FAILED",
    "RUN_FINISHED",
    "RUN_IN_FLIGHT",
    "RUN_ORIGINS",
    "RUN_STARTED",
    "RUN_STATUSES",
    "RUNNING",
    "STOPPED",
    "STOP_QUEUED",
    "STOP_RUNNING",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "RunTransition",
    "active_run_predicate",
]
