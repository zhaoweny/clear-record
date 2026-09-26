"""The SQLite registry: projects and their glossary terms.

One database (SQLAlchemy's SQLite dialect over stdlib :mod:`sqlite3`) holds the
**app-owned** project/glossary state under the platform data directory
(ADR-0025). Audio, workspaces and archives stay as files elsewhere; the registry
stores metadata only (ADR-0007/ADR-0013).

Design notes:

- **The tables are mapped** (ADR-0030). :mod:`clear_record.service.entities`
  declares one entity class per table; the operations below read and write
  through them, and the hand-written row-to-value mapping this module used to
  carry is the entities' columns. What crosses the seam is unchanged: every
  method still returns the frozen dataclasses of
  :mod:`clear_record.service.models`, and no entity, session or statement leaves
  this layer.
- **The statements that must stay Core are Core** (ADR-0030), written as
  SQLAlchemy Core expressions rather than rebuilt out of mapped objects: the run
  claim (:meth:`Registry.claim_run`) and the reconciliation compare-and-set
  (:meth:`Registry.interrupt_run`). Their correctness *is* the statement — the
  write lock decides the claim's winner inside it, and the reconciliation must
  compare the row against the snapshot it judged — so the mapping must not
  decompose either into a read and a write. Three more conditional transitions
  are one Core statement each for the same reason
  (:meth:`Registry.heartbeat_run`, :meth:`Registry.stop_run`,
  :meth:`Registry.request_cancel`), and one statement assigns a run event's
  sequence inside its own insert (:meth:`Registry.add_run_event`). Everything
  else reads and writes through the entities.
- **Every mutation is audited, and the actor is a required argument** (ADR-0033).
  Each mutating method here carries :func:`~clear_record.service.audit.recorded`,
  so one call appends one row to ``audit_event``: who called (the transport's own
  word, never a caller's), what they touched, and how it ended — a refusal
  included, in a unit of work of its own, because the transaction it belonged to
  is rolled back with the failure. A **conditional** write that matched no row (a
  lost claim, a state the move is not legal from), and a **key miss**, append
  nothing, because nothing happened. The table refuses every ``UPDATE`` and
  ``DELETE`` (revision 0010), and :meth:`Registry.record_audit` is the only way a
  row is written at all. The credential's *set* is recorded; the credential and
  session tables' other writes — sign-in, the idle clock, sign-out, the expired
  prune — are bookkeeping and append nothing, because a session is not history of
  the project data and one row per request would bury the record that is.
- **A session per operation.** :meth:`Registry._session` is one unit of work:
  the sessionmaker is bound to the engine this registry owns, the session
  commits on a clean exit, and a session whose body raises is rolled back and
  closed before the exception propagates — so a failed operation leaves the
  registry as it found it. Sessions are never shared or cached: each operation
  opens its own, which keeps the store safe from the web app's threadpool and
  from the queue's concurrent claimants. The engine pools nothing
  (:class:`~sqlalchemy.pool.NullPool`): one connection per unit of work, closed
  with it — the discipline the hand-written connection had. The session
  lifecycle beyond that (a session per request, an app-owned one) is not this
  change's; this is the shape it inherits.
- **Nothing loads lazily.** The entities declare no relationships, so no
  attribute access can fire a query after the session that read the row is gone;
  every join is written out in the query that needs it.
- **Alembic owns the schema, and the registry migrates when it opens**
  (ADR-0030). The revisions live in :mod:`clear_record.service.migrations`; the
  chain begins at the released baseline and an existing registry that stands at
  one is moved forward on open, while one recording a revision this build does
  not carry — or a ladder step no released line stands at — is refused rather
  than silently misread. The migration is forward-only — a revision is upgraded,
  never unwound.
- **Slugs are stable, human-readable ids.** A project is addressable by slug in
  the API, the GUI and any agent context; ids stay internal.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Integer,
    MetaData,
    Select,
    Table,
    Text,
    case,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from clear_record.core.events import JobEvent
from clear_record.core.i18n import tr
from clear_record.core.paths import registry_path
from clear_record.service import audit, entities, tapestore
from clear_record.service.lifecycle import (
    ACTIVE_RUN_STATUSES,
    CLAIM,
    DONE,
    ENQUEUE,
    FAIL,
    INTERRUPT,
    QUEUED,
    RUNNING,
    RUN_ORIGINS,
    RUN_STATUSES,
    STOP_QUEUED,
)
from clear_record.service.models import (
    CANDIDATE,
    MEETING_STATUSES,
    RETIRED,
    TERM_AUTHORS,
    TERM_STATUSES,
    Archive,
    Artifact,
    AuditEvent,
    ConsoleSession,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
    Tape,
)
from clear_record.service.run_options import KEPT_OPTIONS_LEAD, read_run_options

# --- the schema's history, owned by Alembic (ADR-0030) --------------------- #
#
# The revisions in `clear_record.service.migrations` are this registry's schema.
# The chain **begins at the released baseline** — the schema a released install
# has, ``0006`` — and carries only the deltas from it to the head, so a fresh
# registry reaches today's schema without replaying the retired ladder's history.
# An existing registry is placed where it stands — at the released baseline, or
# at any revision the chain still carries — and refused where it does not: a
# ladder row that is a step **other than** the released baseline's, or a revision
# the cut folded away, is a development build's, and no release owes it a
# migration (the tip wipe policy, `docs/releasing.md`).

#: Where the schema's history lives. Named as a package resource rather than a
#: path, so that one value resolves both in a checkout (the member is installed
#: editable) and in an installed wheel; the repository's `alembic.ini` carries
#: the same value for the developer CLI.
_SCRIPT_LOCATION = "clear_record.service:migrations"

#: The **released baselines**: the ladder numbers a *released* install records,
#: and — because a released line's number is also the id of the revision its
#: schema became — the revision ids this chain begins those lines at.
#:
#: **0.2.0 is the only one, and `0.1.x` is not carried.** `v0.2.0` and its
#: release candidates shipped the registry and recorded their schema as ladder
#: version **6**; `v0.1.x` predates the registry entirely — it shipped no
#: registry file and no ladder at all — so it has no version to place and nothing
#: to carry.
#: The number is a **fixed number of the retired ladder**, never derived from the
#: head revision's id: a revision added on top of a baseline (``0007`` onward)
#: must not move the number a released build compares *itself* with, and a head
#: id need not be a number at all — Alembic's own default is a hex uuid, which
#: ``migrations/env.py`` pins away from this chain for exactly this reason.
#: A later cut adds the next released line's baseline here and folds the deltas
#: below it into that baseline's DDL; nothing else about the shape changes.
_RELEASED_BASELINES: tuple[int, ...] = (6,)

#: The retired ladder's last version number, as the ladder itself wrote it.
#:
#: The ladder's own ``SCHEMA_VERSION`` on the trunk when Alembic replaced it —
#: **two** steps past the last released line's 6 (7 and 8 are the trunk's). It is
#: what the levelling write puts in the ladder's row, and it is the bound a row
#: is read against, so it is a **fixed number of the retired ladder** rather than
#: the chain's newest id — see `_LEGACY_VERSION_TABLE` for what the row's readers
#: do with it.
_LADDER_VERSION = 8

#: The retired ladder's version table, and what it is for now.
#:
#: Only a registry that carried it *before* this build has one: the ladder
#: created it, the chain's baseline deliberately does not, so a registry this
#: build creates has no such record. Where it exists, the row is read once to
#: place the stamp and then levelled at :data:`_LADDER_VERSION`.
#:
#: **What the read is for: the released line, and only the released line.** A
#: released registry stands at 6, which is :data:`_RELEASED_BASELINES`' one
#: number, so the row places it at the revision of the same id and the deltas on
#: top run; nothing else the ladder wrote is carried — see
#: :func:`_pending_stamp`. Any other row *at or below* the ladder's last version
#: is a development build's, and the tip wipe policy is the whole
#: repair path for it; a row above that version is a build newer than this one,
#: and the sentence it meets says to upgrade.
#:
#: **What the write is for: the released line's refusal.** A build of the
#: released line reads *this* row and compares it with its own ``SCHEMA_VERSION``,
#: so a migrated registry has to be levelled above that build's number or it
#: would read a schema that is not its own as if it were: at 8 it refuses with
#: "registry schema version 8 is newer than this build supports (6); upgrade
#: clear-record", which is the whole repair path a release can give. The
#: levelling also leaves a **pre-Alembic trunk** build — the build this replaced,
#: whose ladder stopped at :data:`_LADDER_VERSION` — reading the registry instead
#: of re-running its ladder, and that is now a property of the development tree
#: alone: no released build ever reached 8. A registry this build *created*
#: carries no row for that trunk build to read, so it runs its ladder there and
#: dies on v4's ``ALTER TABLE meeting ADD COLUMN notes`` with a raw
#: ``sqlite3.OperationalError: duplicate column name: notes``; seeding this table
#: in the baseline, so that such a registry would reach that build as its own
#: sentence rather than a traceback, was considered and not taken: the row
#: records the ladder history a registry *migrated from the ladder* carries, and
#: a fresh registry has none. The limitation is recorded in ADR-0030.
_LEGACY_VERSION_TABLE = "schema_version"

#: Alembic's own version table.
_ALEMBIC_VERSION_TABLE = "alembic_version"

#: The two version tables, as Core tables rather than mapped entities: they are
#: the *schema history's* bookkeeping — one Alembic's, one the retired ladder's —
#: and not application shapes, so no entity in
#: :mod:`clear_record.service.entities` describes them. Only :meth:`Registry._migrate`
#: reads them, once per open.
_BOOKKEEPING = MetaData()
_ALEMBIC_VERSION = Table(
    _ALEMBIC_VERSION_TABLE, _BOOKKEEPING, Column("version_num", Text, primary_key=True)
)
_LEGACY_VERSION = Table(_LEGACY_VERSION_TABLE, _BOOKKEEPING, Column("version", Integer))


def _has_table(conn: Connection, name: str) -> bool:
    """Whether the registry already carries a table of that name.

    ``inspect`` asks the dialect, and SQLite answers with ``PRAGMA table_info``:
    a *view* of that name answers true as well, where the retired ladder's own
    ``sqlite_master`` lookup counted tables only. No table of this schema is a
    view, so the two agree on every registry there is; the difference is what a
    future view named like a version table would do.
    """
    return inspect(conn).has_table(name)


def _write_ahead_log(dbapi_connection: Any) -> None:
    """Put the registry on SQLite's write-ahead log, once per file.

    The default rollback journal is what makes a *reader* wait behind a writer,
    and lose: a commit holds ``PENDING`` and then ``EXCLUSIVE`` while it writes
    the journal, syncs the database and deletes the journal again, and both
    locks refuse every other connection's ``SHARED`` lock for the whole of that
    — and no busy timeout can help, because there is no wait to be won. The node
    writes continuously (a run appends a report per chunk, the heartbeat
    refreshes a row, and every audited mutation appends its own — ADR-0033), so
    under load those windows tile the timeline, and a reader behind them — the
    console's poll, the CLI's, a test's ``list_run_events`` — waits out the
    whole busy timeout and then fails with ``database is locked``. The
    write-ahead log removes the class instead of the wait: a reader reads the
    last committed snapshot and takes no lock a writer holds, and a commit is
    one append to the log rather than a journal write, two syncs and an unlink.
    Durability is unchanged — the default ``synchronous=FULL`` syncs the log on
    commit, so a committed event or audit row survives a crash — and what the
    log adds on disk is the ``-wal`` and ``-shm`` files beside the registry
    while it is open (both gone once the last connection closes, which
    checkpoints the log back into the database; the pool holds no connection
    between units of work).

    The mode is a property of the **file**, not of the connection, so the read
    below is the whole of the work for every connection after the first to find
    the lock free: ``PRAGMA journal_mode`` answers from the header without
    taking a lock. The conversion itself does need the write lock, and SQLite
    refuses it *without waiting* while another connection holds that lock
    (``SQLITE_BUSY`` at once, not a busy-timeout wait). That refusal is
    tolerated: the connection that meets it works in the mode the file is
    already in, the next connection tries again, and a registry on a filesystem
    that cannot host the log's shared memory keeps working in the rollback
    journal it had rather than failing to open.
    """
    mode = dbapi_connection.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() == "wal":
        return
    try:
        dbapi_connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # The lock belongs to another surface (or the filesystem): the file
        # keeps the mode it has, and the next connection is the next attempt.
        # Nothing here may fail a read or a write.
        return


def _engine(db_path: Path) -> Engine:
    """The engine one registry reads and writes through.

    SQLite keeps the connection's state, not just the database's: foreign-key
    enforcement is off by default and is set per connection, so it is set on
    every connection the engine makes — the guard the store's hand-written
    connection used to carry, now the engine's own. ``recursive_triggers`` is the
    same kind of per-connection state, and it is **defence in depth** for the
    audit record: a ``REPLACE`` is refused here by the table's own insert-side
    guard (``audit_event_no_replace``), which fires before the conflict path runs
    and refuses whatever the connection's settings are; the pragma is the second
    layer, for the delete half of ``REPLACE`` that the insert guard does not cover
    (ADR-0033).

    The pool is :class:`~sqlalchemy.pool.NullPool`, so a connection is made for
    one unit of work and closed with it: the discipline the hand-written
    connection had (connections are cheap; correctness beats pooling), and the
    one that keeps a connection from being handed to a second thread, since the
    console, the web app's threadpool and the MCP server read one registry. The
    busy timeout is the driver's default (``sqlite3.connect``'s five seconds),
    which is what makes a *writer* meet another writer by waiting instead of
    failing; it cannot do the same for a *reader*, which SQLite's default
    rollback journal refuses outright while a commit holds the database. The log
    the listener below applies is the answer to that, and
    :func:`_write_ahead_log` carries the reasoning.
    """
    engine = create_engine(
        URL.create("sqlite", database=str(db_path)), poolclass=NullPool
    )

    @event.listens_for(engine, "connect")
    def _connection_state(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA recursive_triggers = ON")
        _write_ahead_log(dbapi_connection)

    return engine


def _alembic_config(db_path: Path) -> Config:
    """The configuration one registry's migration runs with.

    Built in code rather than read from an ``alembic.ini``, so that an installed
    distribution migrates with no config file beside it; the repository's
    ``alembic.ini`` names the same ``script_location`` and no other option it
    sets differs.
    """
    config = Config()
    config.set_main_option("script_location", _SCRIPT_LOCATION)
    # The path travels as a URL *object*, never as URL text: a `?` in a path is
    # read back out of the text as a query, and the registry that gets migrated
    # is then a different file. `env.py` reads this attribute.
    config.attributes["database_url"] = URL.create("sqlite", database=str(db_path))
    return config


def _revision_history(config: Config) -> tuple[frozenset[str], str, tuple[str, ...]]:
    """Every revision this build carries, its newest, and the chain head-first.

    ``lineage`` is the chain **head-first** — ``walk_revisions`` yields heads
    first, the reverse of the order the steps were applied in — and it is what
    lets a version table holding several rows be reduced to the one that says how
    far the schema actually got: the chain is linear, so the schema carries every
    step up to the *head-most* row, and the lower rows are stale records of steps
    already applied.
    """
    script = ScriptDirectory.from_config(config)
    lineage = tuple(entry.revision for entry in script.walk_revisions())
    return frozenset(lineage), ", ".join(script.get_heads()), lineage


def _stamped_revisions(conn: Connection) -> tuple[str, ...]:
    """The revisions the registry's Alembic version table records."""
    return tuple(row[0] for row in conn.execute(select(_ALEMBIC_VERSION.c.version_num)))


def _reduce_version_rows(
    conn: Connection, rows: tuple[str, ...], lineage: tuple[str, ...]
) -> None:
    """Leave one row in a version table a race gave several.

    Alembic reads more than one row as a **multi-head** state and refuses every
    upgrade from it (``Requested revision 0009 overlaps with other requested
    revisions 0008``), so a registry in this state never opens again — the
    failure two surfaces starting at once left behind while nothing serialized
    them. The rows it wrote record steps that *ran*, the chain is linear, and the
    schema therefore carries every step up to the head-most of them: that row is
    the one kept, the rest are deleted. The write happens inside the migration's
    own transaction, lock held, so a step that then fails rolls the repair back
    with everything else.
    """
    keep = min(rows, key=lineage.index)
    conn.execute(
        _ALEMBIC_VERSION.delete().where(_ALEMBIC_VERSION.c.version_num != keep)
    )


def _pending_stamp(
    conn: Connection,
    db_path: Path,
    known: frozenset[str],
    head: str,
    lineage: tuple[str, ...],
) -> str | None:
    """The baseline a registry that predates this build stands at, if any.

    A registry arrives here in one of six shapes, and five of them are decided
    by what it records:

    - **Alembic has stamped it, at a revision this build carries.** It stands
      there; a race's several rows are reduced to the head-most, and there is no
      stamp to apply. *Carried* means the compressed chain, so a registry a
      *pre-compression* build left at ``0006``–``0009`` belongs here: the
      compression changed no table, and such a registry opens and migrates.
    - **Alembic has stamped it, at a revision this build does not carry.** The
      chain is the schema's history and half-understanding one is worse than
      refusing to open it, so it raises — *upgrade clear-record* when the
      revision's number is above the head's (a build newer than this one), and
      the tip wipe policy's sentence when it is below (a development build's
      revision, which this release's cut folded away).
    - **The released line wrote it** — ``schema_version``, a released line's
      number. :data:`_RELEASED_BASELINES` is the whole of what is carried, and
      the number is the revision id the chain begins that line at, so the
      registry is stamped exactly there and only the deltas on top run. For
      ``0.2.0``, the one released line that ever shipped a registry, that is
      revision ``0006``.
    - **A development build left it behind** — the ladder's row at one of its own
      steps *other than* the released baseline's, or at ``0`` (a ladder run
      killed before it recorded where it got to). Neither is a released baseline,
      and the tip wipe policy is the repair path
      (``docs/releasing.md``): the refusal names the file, because deleting it is
      what the user does about it.
    - **A ladder row above the ladder's last version**, which no build of this
      line ever wrote: a build *newer* than this one, refused with
      *upgrade clear-record* rather than with the wipe sentence — the registry is
      not a development build's, and upgrading is what it needs.
    - **No version state at all.** Either a registry that does not exist yet, or
      one this build created — which carries no ladder row, by decision — so
      every revision runs from the baseline. Running them **is** the repair for a
      stamp that never landed: the baseline only creates (each table
      ``IF NOT EXISTS``), each delta that adds a column is guarded on that column
      (``0007``, ``0008``), and ``0009`` verifies the index it finds instead of
      assuming its own ran, so a replay converges on the schema it started from.

    The one write this read makes is the multi-row repair
    (:func:`_reduce_version_rows`).
    """
    if _has_table(conn, _ALEMBIC_VERSION_TABLE):
        rows = _stamped_revisions(conn)
        for revision in rows:
            if revision in known:
                continue
            # A revision this build does not carry: a *newer* release's, or one
            # this release's cut folded away. The two need different advice, and
            # the ids are decimal by decision (`migrations/env.py`), so the head's
            # own number tells them apart: above it is a newer build, below it a
            # development build's dropped revision.
            if revision.isdigit() and head.isdigit() and int(revision) > int(head):
                raise RuntimeError(
                    tr(
                        "registry schema revision {revision} is not one this build "
                        "carries (its newest is {head}); upgrade clear-record",
                        revision=revision,
                        head=head,
                    )
                )
            raise RuntimeError(
                tr(
                    "the registry at {db_path} records schema revision {revision}, "
                    "which an unreleased development build wrote and this release "
                    "does not carry; delete {db_path} and start clear-record again",
                    db_path=db_path,
                    revision=revision,
                )
            )
        if len(rows) > 1:
            _reduce_version_rows(conn, rows, lineage)
        if rows:
            return None
    if not _has_table(conn, _LEGACY_VERSION_TABLE):
        return None  # no registry here yet: every revision runs from the baseline
    row = conn.execute(select(_LEGACY_VERSION.c.version)).first()
    version = int(row[0]) if row is not None else 0
    # A row above the ladder's own last version is not read as a revision however
    # its four digits happen to read: the ladder's numbers are what a *build from
    # before Alembic* wrote, and 9 upward is a build newer than this one.
    if version > _LADDER_VERSION:
        raise RuntimeError(
            tr(
                "registry schema version {version} is newer than this build "
                "carries (its newest revision is {head}); upgrade clear-record",
                version=version,
                head=head,
            )
        )
    revision = f"{version:04d}"
    if version in _RELEASED_BASELINES and revision in known:
        return revision
    raise RuntimeError(
        tr(
            "the registry at {db_path} was written by an unreleased development "
            "build, which this release does not carry; delete {db_path} and start "
            "clear-record again",
            db_path=db_path,
        )
    )


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _instant(value: _dt.datetime) -> str:
    """A datetime in the registry's text form, microseconds included.

    Everything else the registry stamps keeps the second's precision audit rows
    want; a session's two clocks do not, because the idle timeout is a duration a
    test may shorten below a second — and a deadline that had been rounded to the
    second would make that test's verdict a coin toss.
    """
    return value.astimezone(_dt.UTC).isoformat(timespec="microseconds")


def _parsed_instant(text: str) -> _dt.datetime | None:
    """The datetime :func:`_instant` wrote, or ``None`` for anything else.

    ``None`` rather than an exception: a timestamp this build cannot read is a row
    it cannot judge, and a session row's reader must answer "that session is not
    usable" instead of failing the request it arrived on.
    """
    try:
        value = _dt.datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=_dt.UTC)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _resolved_workspace(path: str) -> str:
    """A workspace path in the one spelling the registry compares by.

    A command-line run names a *directory*, and the same directory can be named
    relatively, with a trailing separator, or through a symlink. Resolving is
    what makes those one meeting rather than a new one per spelling
    (:meth:`Registry.meeting_for_workspace`).
    """
    return str(Path(path).expanduser().resolve())


_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _require_slug(slug: str, what: str) -> str:
    """Validate an explicitly supplied slug: [a-z0-9-]+ only.

    A generated slug is safe by construction; a caller-supplied one is not. The
    managed workspace builds a filesystem path from a meeting's project_slug and
    slug (ADR-0024), so a separator or a '..' component here would escape the
    managed root.
    """
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"{what} slug must match [a-z0-9-]+ (got {slug!r})")
    return slug


# --- the statements the mapping must not rewrite --------------------------- #
#
# The conditional transitions — the claim, the heartbeat, the interruption, the
# stop and the cancel request — are one Core statement each, and that statement
# is where their correctness lives: the ``WHERE`` decides, under the write lock,
# whether the transition happens at all. An ORM-enabled ``UPDATE`` would add a
# ``RETURNING id`` clause to learn which rows it moved (so it can synchronize its
# identity map), and the statement the correctness argument is about would no
# longer be the one emitted. Nothing needs synchronizing: each of these methods
# reads the row back, or reads nothing at all, and holds no object across the
# statement. So they run with synchronization off.
_NO_SYNC = {"synchronize_session": False}


# --- the joins the reads need ---------------------------------------------- #
#
# A meeting and a glossary term carry their project's slug into the boundary
# type, and the slug lives in another table. These joins are written out and
# used by name rather than declared as relationships on the entities: an entity
# that can load another row is an entity that can load it after the session that
# read it is closed.


def _meeting_query() -> Select[tuple[entities.Meeting, str]]:
    """A meeting row joined to its project's slug."""
    return select(entities.Meeting, entities.Project.slug).join(
        entities.Project, entities.Project.id == entities.Meeting.project_id
    )


#: A retire records the status it took the term from, inside ``notes`` — the one
#: field the row has for it, and one no surface is shown (``_term`` strips the
#: marker), so a restore can put a term back where it was without a schema
#: change: a retired candidate comes back a candidate, never owner-accepted.
_RETIRED_FROM = re.compile(r"^\[retired from: (candidate|confirmed)\]\n?", re.MULTILINE)


def _split_retire_marker(notes: str | None) -> tuple[str | None, str | None]:
    """``(the status the term was retired from, the notes without the marker)``."""
    match = _RETIRED_FROM.search(notes or "")
    prior = match.group(1) if match else None
    return prior, _RETIRED_FROM.sub("", notes or "").strip() or None


def _with_retire_marker(notes: str | None, status: str) -> str:
    """``notes`` with the status the term is being retired from recorded in it."""
    _prior, clean = _split_retire_marker(notes)
    marker = f"[retired from: {status}]"
    return f"{marker}\n{clean}" if clean else marker


def _retire_row(row: entities.GlossaryTerm) -> None:
    """Move a term row to :data:`RETIRED`, recording the status it came from.

    The **one** place a term becomes retired, so no path can leave a retired term
    without the marker :meth:`Registry.restore_term` reads back: the retire verb
    and a status change to ``retired`` (``update_term(status=…)``, the console's
    status control and the API's ``PATCH``) both land here. A term already
    retired keeps the marker it has — a second retire must not overwrite the
    status the *first* one took it from.
    """
    if row.status != RETIRED:
        row.notes = _with_retire_marker(row.notes, row.status)
    row.status = RETIRED


def _term_query() -> Select[tuple[entities.GlossaryTerm, str]]:
    """A glossary term row joined to its project's slug."""
    return select(entities.GlossaryTerm, entities.Project.slug).join(
        entities.Project, entities.Project.id == entities.GlossaryTerm.project_id
    )


#: The console credential's one row. The table's ``CHECK`` says the same thing
#: in the file: one install has one credential, so a *second* row is refused by
#: the schema rather than by whichever statement remembered to look.
_CREDENTIAL_ROW = 1


class RegistryLocked(RuntimeError):
    """Another surface holds the registry's write lock while it migrates.

    The registry migrates when it opens (ADR-0030), and two surfaces share one
    file, so one open can meet another's migration.
    :meth:`Registry._migrate` takes SQLite's write lock for the whole step, so
    this is what a lock still held after the driver's busy timeout means — the
    other surface is mid-step, and this open did not wait past its timeout. It is
    a *transient* state with one remedy, so the message names the file and says
    so; the exception it came from stays chained as the cause.
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(
            tr(
                "another surface is migrating the registry at {db_path} and still "
                "holds its write lock; nothing was changed — retry",
                db_path=db_path,
            )
        )


class Registry:
    """The single owner of the SQLite registry and its read/writes."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # One engine and one sessionmaker per registry. The engine holds no
        # connection *between* units of work (``NullPool``) — its pool is empty
        # again as soon as a call returns — while the migration below opens
        # connections of its own, so building a registry is what creates the
        # database file and brings it to this build's schema.
        self._engine = _engine(self.db_path)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._migrate()

    @classmethod
    def open(
        cls,
        data_dir: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> Registry:
        """Open (creating if needed) the registry at the resolved location."""
        return cls(db_path if db_path is not None else registry_path(data_dir))

    # --- connection / schema ---------------------------------------------- #
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """One unit of work: the session commits on a clean exit.

        The busy timeout is the driver's default, which is also what makes a
        second *process* (the console and an agent's MCP server share one
        registry) wait for a writer instead of raising ``database is
        locked`` — for a writer. A reader is not promised that wait: while a
        commit holds the database, the rollback journal refuses another
        connection's read lock outright, so no timeout can win it (see
        :func:`_write_ahead_log`, which the engine applies to every connection).
        What the driver *does* keep here is the hand-written connection's
        promise about the other shape SQLite refuses to wait for: pysqlite
        begins the transaction when the first write executes, not when the
        session reads, so a read followed by a write still reaches the write
        lock holding no read lock, and the write waits on the busy timeout
        rather than failing.

        A session whose body raises is rolled back and closed, and the exception
        propagates unchanged: a ``ValueError`` or ``KeyError`` is the one the
        method documents, and a failure from the database arrives as SQLAlchemy's
        own error (``IntegrityError`` on a duplicate, ``OperationalError`` on a
        locked database), which wraps the driver's — not as the ``sqlite3`` class
        a caller may have caught before this mapping (see ADR-0030). No
        half-written unit of work outlives it. ``expire_on_commit`` is off, so
        the values a method read stay readable while it builds the dataclass it
        returns, and nothing re-queries for them.
        """
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def _migrate(self) -> None:
        """Bring the registry to the schema this build carries, as it opens.

        An existing registry that stands at a released baseline moves forward
        here: this is the auto-migration the hand-rolled ladder used to do, now
        Alembic's, and a registry this build created is already at the head and
        has nothing to run. A registry recording a revision this build does not
        carry — or a ladder version that is no released baseline — is refused
        first (see :func:`_pending_stamp`) — before any revision runs, and
        before anything reads it as its own.

        **One opener at a time, from the first read.** The registry is shared by
        the console and an agent's MCP server, and both migrate at open,
        so the whole step — the version tables' read, the stamp, the revisions
        and the ladder row — runs on **one connection holding SQLite's write
        lock from before that read** (``BEGIN IMMEDIATE``), and the revisions run
        on that same connection (``migrations/env.py`` takes it from the
        configuration). Two openers therefore take the lock in turn instead of
        interleaving, and one that fails rolls back to exactly the registry the
        other left. Without it the retired ladder's ``CREATE TABLE IF NOT
        EXISTS`` steps tolerated two openers and Alembic's do not: the loser died
        on the version table's own DDL (``table alembic_version already exists``)
        or, worse, left that table recording two revisions, which no later open
        can read (see :func:`_reduce_version_rows`).
        """
        config = _alembic_config(self.db_path)
        known, head, lineage = _revision_history(config)
        with self._engine.connect() as conn:
            # The write lock, taken before anything is read: SQLite refuses to
            # upgrade a read transaction, and a decision read outside the lock is
            # a decision another opener can invalidate before it is written.
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as exc:
                # The lock belongs to another surface and it is still migrating:
                # transient, so the message says what to do instead of letting a
                # traceback speak for it.
                raise RegistryLocked(self.db_path) from exc
            try:
                legacy = _has_table(conn, _LEGACY_VERSION_TABLE)
                stamp = _pending_stamp(conn, self.db_path, known, head, lineage)
                # The revisions run on this connection, inside this lock.
                config.attributes["connection"] = conn
                if stamp is not None:
                    # A registry the released line wrote: it already stands at
                    # that baseline, so record where it is and let the upgrade
                    # below run only the deltas it is missing.
                    command.stamp(config, stamp)
                command.upgrade(config, "head")
                if legacy:
                    # Level the ladder's row at the ladder's own last version —
                    # the number a build from before Alembic compares itself
                    # with, and the one that lets it treat the ladder as already
                    # applied (see `_LEGACY_VERSION_TABLE`).
                    conn.execute(
                        update(_LEGACY_VERSION).values(version=_LADDER_VERSION)
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                config.attributes.pop("connection", None)

    # --- the audit record (ADR-0033) --------------------------------------- #
    def record_audit(
        self, actor: str, action: str, target: str, outcome: str = audit.OK
    ) -> None:
        """Append one row to the audit record — the **only** way a row is written.

        ``actor`` is the surface's own word for itself
        (:data:`~clear_record.service.lifecycle.ACTORS`), ``action`` the verb it
        performed, ``target`` the service's address for what it touched, and
        ``outcome`` whether the call did what it was asked (:data:`~.audit.OK`) or
        was refused (:data:`~.audit.FAILED`).

        There is deliberately no update and no delete beside it, and revision 0010
        makes that hold past this class: triggers on the table refuse every
        ``UPDATE`` and every ``DELETE``, so a record of who did what cannot be
        rewritten by the code that wrote it. The row is appended in a unit of work
        of its own — which is what lets a **refused** call be recorded at all, the
        transaction it belonged to having been rolled back with the failure
        (:func:`clear_record.service.audit.recorded`).

        An actor outside the vocabulary is refused *before* the row is attempted:
        the word a surface supplies is a value the rest of the service reads, not
        a free-form label.
        """
        actor = audit.require_actor(actor)
        if outcome not in audit.OUTCOMES:
            raise ValueError(
                f"unknown audit outcome {outcome!r}; expected one of "
                + ", ".join(audit.OUTCOMES)
            )
        with self._session() as session:
            session.add(
                entities.AuditEvent(
                    at=_now(),
                    actor=actor,
                    action=action,
                    target=target,
                    outcome=outcome,
                )
            )

    def list_audit_events(self, *, limit: int | None = None) -> list[AuditEvent]:
        """The audit record, oldest row first — the order it was written in."""
        with self._session() as session:
            statement = select(entities.AuditEvent).order_by(entities.AuditEvent.id)
            if limit is not None:
                statement = statement.limit(limit)
            return [self._audit_event(row) for row in session.scalars(statement)]

    # --- the console credential and its sessions (the auth gate) ----------- #
    #
    # Two operational tables, not project data: the credential that gates the
    # console and the server-side sessions a sign-in opens. They live in the
    # registry because that is where the app's own state lives and because they
    # must survive a restart — a session in process memory would be a session a
    # restart forgives — and they carry no secret: the credential is a salted
    # hash and a session row is a token's digest
    # (:mod:`clear_record.service.auth`). Only the credential's *set* appends an
    # audit row; the session rows are bookkeeping (sign-in, the idle clock,
    # sign-out, the prune) and are excluded for the reason the setup marker is:
    # they are not history of what the operator's data did.

    def credential(self) -> str | None:
        """The stored credential hash, or ``None`` when none has been set.

        The registry's answer to the first run's one question. It is read on the
        sign-in path too, so the value never leaves the process except into
        :func:`~clear_record.service.auth.verify_password`.
        """
        with self._session() as session:
            return session.scalar(select(entities.ConsoleCredential.encoded))

    @audit.recorded("credential.set", "credential:console")
    def store_credential(self, encoded: str, *, actor: str) -> None:
        """Set or replace the console's one credential row.

        One row, id :data:`_CREDENTIAL_ROW`: the schema's ``CHECK`` makes a second
        impossible, so "there is one credential" is the file's own rule and not a
        convention this method has to keep. The previous value is overwritten —
        it was a hash, and a history of hashes is a list of things to attack
        rather than a record of anything.
        """
        with self._session() as session:
            row = session.get(entities.ConsoleCredential, _CREDENTIAL_ROW)
            if row is None:
                session.add(
                    entities.ConsoleCredential(
                        id=_CREDENTIAL_ROW, encoded=encoded, updated_at=_now()
                    )
                )
            else:
                row.encoded = encoded
                row.updated_at = _now()

    def create_session(
        self,
        digest: str,
        *,
        created_at: _dt.datetime,
        seen_at: _dt.datetime,
        idle_deadline: _dt.datetime,
        absolute_deadline: _dt.datetime,
    ) -> None:
        """Record one signed-in session, keyed by its token's digest.

        Four instants and no identity: which *human* this is was settled when the
        credential was checked, and there is one human (ADR-0033). The deadlines
        are computed by the caller's policy — this layer stores what it is told.
        """
        with self._session() as session:
            session.add(
                entities.ConsoleSession(
                    token_digest=digest,
                    created_at=_instant(created_at),
                    seen_at=_instant(seen_at),
                    idle_deadline=_instant(idle_deadline),
                    absolute_deadline=_instant(absolute_deadline),
                )
            )

    def get_session(self, digest: str) -> ConsoleSession | None:
        """The session ``digest`` names, or ``None``.

        A row whose timestamps this build cannot read answers ``None`` as well: it
        is not a session anyone can be inside, and the request it arrived on is
        answered rather than failed.
        """
        with self._session() as session:
            row = session.get(entities.ConsoleSession, digest)
            return None if row is None else self._console_session(row)

    def touch_session(
        self,
        digest: str,
        *,
        seen_at: _dt.datetime,
        idle_deadline: _dt.datetime,
    ) -> None:
        """Move a live session's idle clock forward — the write an accepted request makes."""
        with self._session() as session:
            session.execute(
                update(entities.ConsoleSession)
                .where(entities.ConsoleSession.token_digest == digest)
                .values(
                    seen_at=_instant(seen_at), idle_deadline=_instant(idle_deadline)
                )
            )

    def end_session(self, digest: str) -> bool:
        """Delete the one session ``digest`` names; True when a row was there.

        A conditional write, read off its own ``rowcount``: a cookie naming a
        session that is already gone ends nothing, and there is nothing to say
        about it.
        """
        with self._session() as session:
            result = session.execute(
                delete(entities.ConsoleSession).where(
                    entities.ConsoleSession.token_digest == digest
                )
            )
            return bool(result.rowcount)

    def end_all_sessions(self) -> int:
        """Delete every session; the count of rows that were there."""
        with self._session() as session:
            result = session.execute(delete(entities.ConsoleSession))
            return int(result.rowcount or 0)

    def prune_expired_sessions(self, now: _dt.datetime) -> int:
        """Delete every session past a deadline, and every unreadable row.

        Housekeeping, not an act: these sessions are already over — the next
        request bearing one is refused by the same test this delete applies — so
        the write only keeps the table from growing a row per sign-in forever. A
        row whose timestamps do not parse is deleted here too: nothing can be
        inside it.
        """
        dead: list[str] = []
        with self._session() as session:
            for row in session.scalars(select(entities.ConsoleSession)):
                session_row = self._console_session(row)
                if (
                    session_row is None
                    or session_row.idle_deadline <= now
                    or session_row.absolute_deadline <= now
                ):
                    dead.append(row.token_digest)
            if dead:
                session.execute(
                    delete(entities.ConsoleSession).where(
                        entities.ConsoleSession.token_digest.in_(dead)
                    )
                )
        return len(dead)

    # --- projects ---------------------------------------------------------- #
    @staticmethod
    def _project_taken(session: Session, slug: str) -> bool:
        """Whether a project already answers to ``slug`` (the collision probe)."""
        return (
            session.scalar(
                select(entities.Project.slug).where(entities.Project.slug == slug)
            )
            is not None
        )

    def _unique_slug(self, session: Session, name: str) -> str:
        base = _slugify(name)
        slug, n = base, 2
        while self._project_taken(session, slug):
            slug = f"{base}-{n}"
            n += 1
        return slug

    @audit.recorded("project.create", audit.subject("project", "slug", "name"))
    def create_project(
        self,
        name: str,
        notes: str = "",
        default_archive_root: str | None = None,
        slug: str | None = None,
        *,
        actor: str,
    ) -> Project:
        name = name.strip()
        if not name:
            raise ValueError("project name must not be blank")
        explicit = (slug or "").strip()
        if explicit:
            _require_slug(explicit, "project")
        with self._session() as session:
            slug = explicit or self._unique_slug(session, name)
            project = entities.Project(
                slug=slug,
                name=name,
                notes=notes,
                default_archive_root=default_archive_root,
                created_at=_now(),
            )
            session.add(project)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError(f"project slug {slug!r} already exists") from exc
            return self._project(project)

    def list_projects(self) -> list[Project]:
        with self._session() as session:
            rows = session.scalars(
                select(entities.Project).order_by(
                    entities.Project.name.collate("NOCASE")
                )
            )
            return [self._project(row) for row in rows]

    def get_project(self, slug: str) -> Project | None:
        with self._session() as session:
            row = session.scalar(
                select(entities.Project).where(entities.Project.slug == slug)
            )
            return self._project(row) if row is not None else None

    def require_project(self, slug: str) -> Project:
        project = self.get_project(slug)
        if project is None:
            raise KeyError(slug)
        return project

    @audit.recorded("project.update", audit.subject("project", "slug"))
    def update_project(
        self,
        slug: str,
        *,
        actor: str,
        name: str | None = None,
        notes: str | None = None,
        default_archive_root: str | None = None,
    ) -> Project:
        fields: dict[str, object] = {}
        if name is not None:
            if not name.strip():
                raise ValueError("project name must not be blank")
            fields["name"] = name.strip()
        if notes is not None:
            fields["notes"] = notes
        if default_archive_root is not None:
            fields["default_archive_root"] = default_archive_root
        with self._session() as session:
            project = session.scalar(
                select(entities.Project).where(entities.Project.slug == slug)
            )
            if project is None:
                raise KeyError(slug)
            for key, value in fields.items():
                setattr(project, key, value)
            return self._project(project)

    def term_counts(self) -> dict[str, int]:
        """Per-project term count, keyed by project slug (for list views)."""
        with self._session() as session:
            rows = session.execute(
                select(entities.Project.slug, func.count(entities.GlossaryTerm.id))
                .outerjoin(
                    entities.GlossaryTerm,
                    entities.GlossaryTerm.project_id == entities.Project.id,
                )
                .group_by(entities.Project.id)
            )
            return {slug: int(count) for slug, count in rows}

    # --- glossary ---------------------------------------------------------- #
    @audit.recorded("term.add", audit.subject("term", "term"))
    def add_term(
        self,
        project_slug: str,
        term: str,
        *,
        actor: str,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str = "candidate",
        added_by: str = "human",
        notes: str | None = None,
    ) -> GlossaryTerm:
        term = term.strip()
        if not term:
            raise ValueError("term must not be blank")
        if status not in TERM_STATUSES:
            raise ValueError(f"status must be one of {TERM_STATUSES}, got {status!r}")
        if added_by not in TERM_AUTHORS:
            raise ValueError(
                f"added_by must be one of {TERM_AUTHORS}, got {added_by!r}"
            )
        project = self.require_project(project_slug)
        with self._session() as session:
            row = entities.GlossaryTerm(
                project_id=project.id,
                term=term,
                reading=reading,
                aliases=aliases,
                definition=definition,
                status=status,
                added_by=added_by,
                notes=notes,
                created_at=_now(),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError(
                    f"term {term!r} already exists in {project_slug!r}"
                ) from exc
            return self._term(row, project_slug)

    def list_terms(
        self, project_slug: str | None = None, status: str | None = None
    ) -> list[GlossaryTerm]:
        if status is not None and status not in TERM_STATUSES:
            raise ValueError(f"status must be one of {TERM_STATUSES}, got {status!r}")
        stmt = _term_query().order_by(
            entities.Project.name.collate("NOCASE"),
            entities.GlossaryTerm.term.collate("NOCASE"),
        )
        if project_slug is not None:
            stmt = stmt.where(entities.Project.slug == project_slug)
        if status is not None:
            stmt = stmt.where(entities.GlossaryTerm.status == status)
        with self._session() as session:
            return [self._term(term, slug) for term, slug in session.execute(stmt)]

    @audit.recorded("term.update", audit.subject("term", "term_id"))
    def update_term(
        self,
        term_id: int,
        *,
        actor: str,
        term: str | None = None,
        reading: str | None = None,
        aliases: str | None = None,
        definition: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> GlossaryTerm:
        fields: dict[str, object] = {}
        if term is not None:
            if not term.strip():
                raise ValueError("term must not be blank")
            fields["term"] = term.strip()
        for key, value in (
            ("reading", reading),
            ("aliases", aliases),
            ("definition", definition),
            ("notes", notes),
        ):
            if value is not None:
                fields[key] = value
        if status is not None:
            if status not in TERM_STATUSES:
                raise ValueError(
                    f"status must be one of {TERM_STATUSES}, got {status!r}"
                )
            fields["status"] = status
        with self._session() as session:
            row, project_slug = self._term_entity(session, term_id)
            for key, value in fields.items():
                if key == "status" and value == RETIRED:
                    # The status control can retire a term too, so it takes the
                    # same path as the retire verb: a retired term without the
                    # marker its restore reads back would come back a candidate.
                    _retire_row(row)
                    continue
                setattr(row, key, value)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("a term with that spelling already exists") from exc
            return self._term(row, project_slug)

    @audit.recorded("term.retire", audit.subject("term", "term_id"))
    def retire_term(self, term_id: int, *, actor: str) -> GlossaryTerm:
        """Retire a term: it leaves the decoder's glossary, the row survives.

        Deleting the row would lose who added it and when, so a retire is a
        status change (ADR-0033). The status it came from is recorded on the row
        (inside ``notes``, which no surface is shown) so :meth:`restore_term` can
        put it back: a retired candidate returns as a candidate, never as
        owner-accepted truth. ``actor`` is the transport's word for the surface
        that retired it, recorded with the move (ADR-0033).
        """
        with self._session() as session:
            row, project_slug = self._term_entity(session, term_id)
            _retire_row(row)
            return self._term(row, project_slug)

    @audit.recorded("term.restore", audit.subject("term", "term_id"))
    def restore_term(self, term_id: int, *, actor: str) -> GlossaryTerm:
        """Return a retired term to the status it held before the retire.

        A term whose recorded status is gone (a later edit overwrote ``notes``)
        comes back a ``candidate`` — the un-reviewed state, never
        owner-accepted. Only a **retired** term can be restored: restoring one
        that is not retired would invent a status (a confirmed term would fall
        back to candidate, dropping owner-accepted truth out of the decoder's
        bias), so that call is refused with the status the term actually holds.
        ``actor`` is the transport's word for the surface that restored it,
        recorded with the move (ADR-0033).
        """
        with self._session() as session:
            row, project_slug = self._term_entity(session, term_id)
            if row.status != RETIRED:
                raise ValueError(
                    f"term {row.term!r} is {row.status}, not retired; there is "
                    "nothing to restore"
                )
            prior, clean = _split_retire_marker(row.notes)
            row.status = prior or CANDIDATE
            row.notes = clean
            return self._term(row, project_slug)

    def get_term(self, term_id: int) -> GlossaryTerm | None:
        with self._session() as session:
            row = session.execute(
                _term_query().where(entities.GlossaryTerm.id == term_id)
            ).first()
            if row is None:
                return None
            term, project_slug = row
            return self._term(term, project_slug)

    # --- meetings ---------------------------------------------------------- #
    def _unique_meeting_slug(
        self, session: Session, project_id: int, title: str
    ) -> str:
        base = _slugify(title)
        slug, n = base, 2
        while (
            session.scalar(
                select(entities.Meeting.slug).where(
                    entities.Meeting.project_id == project_id,
                    entities.Meeting.slug == slug,
                )
            )
            is not None
        ):
            slug = f"{base}-{n}"
            n += 1
        return slug

    @audit.recorded("meeting.create", audit.subject("meeting", "slug", "title"))
    def create_meeting(
        self,
        project_slug: str,
        title: str,
        *,
        actor: str,
        recorded_at: str | None = None,
        workspace_path: str | None = None,
        slug: str | None = None,
    ) -> Meeting:
        project = self.require_project(project_slug)
        title = title.strip()
        if not title:
            raise ValueError("meeting title must not be blank")
        explicit = (slug or "").strip()
        if explicit:
            _require_slug(explicit, "meeting")
        with self._session() as session:
            slug = explicit or self._unique_meeting_slug(session, project.id, title)
            row = entities.Meeting(
                project_id=project.id,
                slug=slug,
                title=title,
                recorded_at=recorded_at,
                workspace_path=workspace_path,
                notes="",
                status="new",
                created_at=_now(),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError(
                    f"meeting slug {slug!r} already exists in {project_slug!r}"
                ) from exc
            return self._meeting(row, project_slug)

    def list_meetings(self, project_slug: str | None = None) -> list[Meeting]:
        stmt = _meeting_query().order_by(
            func.coalesce(
                entities.Meeting.recorded_at, entities.Meeting.created_at
            ).desc(),
            entities.Meeting.id.desc(),
        )
        if project_slug is not None:
            stmt = stmt.where(entities.Project.slug == project_slug)
        with self._session() as session:
            return [self._meeting(row, slug) for row, slug in session.execute(stmt)]

    def get_meeting(self, project_slug: str, meeting_slug: str) -> Meeting | None:
        with self._session() as session:
            row = session.execute(
                _meeting_query().where(
                    entities.Project.slug == project_slug,
                    entities.Meeting.slug == meeting_slug,
                )
            ).first()
            if row is None:
                return None
            meeting, slug = row
            return self._meeting(meeting, slug)

    def require_meeting(self, project_slug: str, meeting_slug: str) -> Meeting:
        meeting = self.get_meeting(project_slug, meeting_slug)
        if meeting is None:
            raise KeyError(f"{project_slug}/{meeting_slug}")
        return meeting

    def meeting_by_id(self, meeting_id: int) -> Meeting | None:
        with self._session() as session:
            row = session.execute(
                _meeting_query().where(entities.Meeting.id == meeting_id)
            ).first()
            if row is None:
                return None
            meeting, project_slug = row
            return self._meeting(meeting, project_slug)

    @audit.recorded("meeting.status", audit.subject("meeting", "meeting_id"))
    def set_meeting_status(
        self, meeting_id: int, status: str, *, actor: str
    ) -> Meeting:
        if status not in MEETING_STATUSES:
            raise ValueError(
                f"status must be one of {MEETING_STATUSES}, got {status!r}"
            )
        with self._session() as session:
            row, project_slug = self._meeting_entity(session, meeting_id)
            row.status = status
            return self._meeting(row, project_slug)

    @audit.recorded("meeting.workspace", audit.subject("meeting", "meeting_id"))
    def set_meeting_workspace(
        self, meeting_id: int, workspace_path: str, *, actor: str
    ) -> Meeting:
        with self._session() as session:
            row, project_slug = self._meeting_entity(session, meeting_id)
            row.workspace_path = workspace_path
            return self._meeting(row, project_slug)

    @audit.recorded("meeting.update", audit.subject("meeting", "meeting_id"))
    def update_meeting(
        self,
        meeting_id: int,
        *,
        actor: str,
        title: str | None = None,
        notes: str | None = None,
    ) -> Meeting:
        """Update a meeting's title and/or notes (the user/agent's story).

        ``None`` leaves a field untouched; an empty string clears notes. The
        workspace and status change through their own operations.
        """
        fields: dict[str, object] = {}
        if title is not None:
            if not title.strip():
                raise ValueError("meeting title must not be blank")
            fields["title"] = title.strip()
        if notes is not None:
            fields["notes"] = notes
        with self._session() as session:
            row, project_slug = self._meeting_entity(session, meeting_id)
            for key, value in fields.items():
                setattr(row, key, value)
            return self._meeting(row, project_slug)

    # --- recording sets ---------------------------------------------------- #
    @audit.recorded("meeting.tapes", audit.subject("meeting", "meeting_id"))
    def set_recording_set(
        self, meeting_id: int, paths: list[str] | tuple[str, ...], *, actor: str
    ) -> RecordingSet:
        clean = [str(p) for p in paths if str(p).strip()]
        if not clean:
            raise ValueError("a recording set needs at least one tape")
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._session() as session:
            return self._recording_set(
                tapestore.write_set(session, meeting_id, clean, _now())
            )

    def latest_recording_set(self, meeting_id: int) -> RecordingSet | None:
        with self._session() as session:
            latest = tapestore.latest_set(session, meeting_id)
            return self._recording_set(latest) if latest is not None else None

    # --- a workspace directory's meeting ----------------------------------- #
    def meeting_for_workspace(self, directory: str, *, actor: str) -> Meeting:
        """The meeting this node runs *directory* as — found, or registered here.

        The command line's subject is a workspace **directory** (``--dir``) and
        this node's subjects are meetings, so this method is that one
        translation, in one place (ADR-0032): the meeting whose
        ``workspace_path`` *is* the directory — compared by resolved path, so a
        relative, trailing-separator or symlinked spelling is the same
        workspace — or, when the node has none, a meeting registered under the
        project its directory names and titled after it.

        A meeting registered here carries ``recorded_at=None`` and status
        ``new``, exactly as the console's own create does: what a directory holds
        is not a date, and the run that follows is what moves the meeting on. Its
        **tapes are set by the run path**, not here: the registry stores metadata
        and never walks a workspace, so ``runs.workspace_run_meeting`` is what
        reads the directory's audio into the meeting's tape set.

        **Two surfaces registering one folder at once are a real pair** — the
        command line's ``run <dir>`` and the console adding the same folder, say —
        and neither the scan nor the creates above can stop the other writer: the
        project's slug and the meeting's are each unique, so the write that lands
        second is refused. That refusal is not an error to report: the folder is
        registered, which is what the call asked for, so the loser looks once more
        for the meeting the winner wrote and answers with it. A ``ValueError``
        that scan does not explain is re-raised as itself.
        """
        resolved = _resolved_workspace(directory)
        meeting = self._meeting_at(resolved)
        if meeting is not None:
            return meeting
        name = Path(resolved).name or resolved
        try:
            project = self.get_project(_slugify(name)) or self.create_project(
                name, actor=actor
            )
            return self.create_meeting(
                project.slug, name, workspace_path=resolved, actor=actor
            )
        except ValueError:
            # The rollback journal used to decide this race by accident: its
            # commit excludes readers, so the two registrations were serialized
            # and the second one's read found the first one's row. The
            # write-ahead log does not serialize them (:func:`_write_ahead_log`),
            # which leaves the loser the job the codebase already gives every
            # read-then-write pair — read again, and answer with what the winner
            # wrote (``runs.RunManager.start`` reads the same way over the
            # one-active-run index).
            meeting = self._meeting_at(resolved)
            if meeting is None:
                raise
            return meeting

    def _meeting_at(self, resolved: str) -> Meeting | None:
        """The meeting registered for a resolved workspace path, or ``None``."""
        for meeting in self.list_meetings():
            if meeting.workspace_path and (
                _resolved_workspace(meeting.workspace_path) == resolved
            ):
                return meeting
        return None

    # --- uploaded tapes ---------------------------------------------------- #
    @audit.recorded("tape.register", audit.subject("meeting", "meeting_id"))
    def register_tape(
        self,
        meeting_id: int,
        *,
        actor: str,
        path: str,
        sha256: str,
        bytes: int,
    ) -> Tape:
        """Record an uploaded tape **and** add it to the meeting's tape set.

        One transaction, so the integrity facts and the tape set the pipeline
        reads can never disagree: the path is appended to the latest set (or a
        first set is created), then the tape row is written. The edit is
        :mod:`clear_record.service.tapestore`'s (the one owner of a meeting's
        tape storage); this method owns the guard and the transaction.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._session() as session:
            return self._tape(
                tapestore.record_tape(
                    session,
                    meeting_id,
                    path=path,
                    sha256=sha256,
                    size=bytes,
                    created_at=_now(),
                )
            )

    def list_tapes(self, meeting_id: int) -> list[Tape]:
        with self._session() as session:
            rows = session.scalars(
                select(entities.Tape)
                .where(entities.Tape.meeting_id == meeting_id)
                .order_by(entities.Tape.id)
            )
            return [self._tape(row) for row in rows]

    def get_tape(self, tape_id: int) -> Tape | None:
        with self._session() as session:
            row = session.get(entities.Tape, tape_id)
            return self._tape(row) if row is not None else None

    @audit.recorded("tape.forget", audit.subject("tape", "tape_id"))
    def forget_tape(self, tape_id: int, *, actor: str) -> Tape:
        """Drop a tape's row and remove its path from the meeting's tape set.

        The file is the caller's to unlink (the store owns no filesystem). When
        the deleted tape was the meeting's last one, the tape set is cleared
        rather than left pointing at a file that no longer exists. The edit is
        :mod:`clear_record.service.tapestore`'s, in this method's transaction.
        """
        with self._session() as session:
            row = session.get(entities.Tape, tape_id)
            if row is None:
                raise KeyError(tape_id)
            tape = self._tape(row)
            tapestore.forget_tape(session, tape, created_at=_now())
            return tape

    # --- pipeline runs ----------------------------------------------------- #
    @audit.recorded("run.enqueue", audit.subject("meeting", "meeting_id"))
    def create_run(
        self,
        meeting_id: int,
        *,
        actor: str,
        backend: str | None = None,
        model: str | None = None,
        language: str | None = None,
        options: dict | None = None,
        run_options: dict | None = None,
        origin: str | None = None,
        resumes_run_id: int | None = None,
    ) -> PipelineRun:
        """Create a **queued** run at the back of the node's FIFO.

        ``options`` is run meta (e.g. the glossary snapshot identity) recorded so
        a re-run can be explained later. ``run_options`` is the resolved
        :class:`~clear_record.core.PipelineOptions` the run will execute with,
        recorded so a queued run is picked back up after a restart; it is the
        whole options value (``dataclasses.asdict`` of it), because the registry
        validates what it reads back against that shape and refuses a partial
        row (`service.run_options`). ``origin`` is
        the surface that started it, one of :data:`RUN_ORIGINS` (RUN-02); it is
        ``None`` only for a caller that is not a start path (a seeded row).

        ``resumes_run_id`` links this run to the run it continues (RUN-04). The
        reference is checked here rather than left to the reader: a link to a run
        that does not exist, or to a run of another meeting, would be a lie the
        registry itself could see.

        One active run per meeting is the table's own rule (revision 0009's
        partial unique index), not this method's: an insert that would give the
        meeting a second ``queued`` or ``running`` run raises
        :class:`~sqlalchemy.exc.IntegrityError` here rather than landing, and the
        caller that owns the user-facing refusal — the run manager, whose guard
        read the same rule
        (:func:`~clear_record.service.lifecycle.active_run_predicate`) —
        translates it. A meeting's *history* is unaffected: as many ended runs as
        it has had.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        if origin is not None and origin not in RUN_ORIGINS:
            raise ValueError(f"origin must be one of {RUN_ORIGINS}, got {origin!r}")
        if resumes_run_id is not None:
            previous = self.get_run(resumes_run_id)
            if previous is None:
                raise KeyError(resumes_run_id)
            if previous.meeting_id != meeting_id:
                raise ValueError(
                    f"run {resumes_run_id} belongs to meeting "
                    f"{previous.meeting_id}, not {meeting_id}"
                )
        with self._session() as session:
            row = entities.PipelineRun(
                meeting_id=meeting_id,
                status=ENQUEUE.target,
                backend=backend,
                model=model,
                language=language,
                options=json.dumps(options) if options else None,
                started_at=None,
                ended_at=None,
                error=None,
                progress=None,
                created_at=_now(),
                run_options=json.dumps(run_options) if run_options else None,
                origin=origin,
                owner=None,
                heartbeat_at=None,
                resumes_run_id=resumes_run_id,
                cancel_requested_at=None,
            )
            session.add(row)
            session.flush()
            return self._run(row)

    @audit.recorded("run.update", audit.subject("run", "run_id"))
    def update_run(
        self,
        run_id: int,
        *,
        actor: str,
        status: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        error: str | None = None,
        progress: dict | None = None,
    ) -> PipelineRun:
        """Update a run's mutable fields.

        ``progress`` is the terminal summary written when a run ends. Its
        ``cost`` is the run's raw cost record (RUN-01), which the console and
        the history-based ETA read back through :class:`PipelineRun`.

        ``status='done'`` — the state
        :data:`~clear_record.service.lifecycle.FINISH` lands in — also **clears**
        ``error``: a run that reached its own successful end reports no error,
        whatever a reaper wrote on it while wrongly believing the owner dead —
        the reason would otherwise sit on a finished run and be shown as a failure
        by the console and the API. There is no caller that wants both, so the
        clear wins over a passed ``error``.

        This is the **unconditional** terminal write: the run's own owner ends the
        row it holds, so the target is validated against the vocabulary
        (:data:`~clear_record.service.lifecycle.RUN_STATUSES`) and the state the
        row leaves is not constrained. The transitions whose legality the database
        enforces are the conditional statements — :meth:`claim_run`,
        :meth:`stop_run`, :meth:`interrupt_run`, :meth:`fail_unreadable_run` — and
        they read their move's own sources.
        """
        fields: dict[str, object] = {}
        if status is not None:
            if status not in RUN_STATUSES:
                raise ValueError(
                    f"status must be one of {RUN_STATUSES}, got {status!r}"
                )
            fields["status"] = status
        if started_at is not None:
            fields["started_at"] = started_at
        if ended_at is not None:
            fields["ended_at"] = ended_at
        if error is not None:
            fields["error"] = error
        if progress is not None:
            fields["progress"] = json.dumps(progress)
        if status == DONE:
            fields["error"] = None
        with self._session() as session:
            row = session.get(entities.PipelineRun, run_id)
            if row is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                setattr(row, key, value)
            return self._run(row)

    @audit.recorded("run.unreadable", audit.subject("run", "run_id"), conditional=True)
    def fail_unreadable_run(self, run_id: int, *, actor: str, error: str) -> bool:
        """Fail a run whose stored options this build refuses to read.

        The one write in this class that does **not** map its row back to a value.
        Every other run write returns the :class:`PipelineRun` it stored, and that
        mapping is exactly what such a row fails — so a write that read it back
        would be rolled back with the exception (a session whose body raises
        commits nothing), leaving the row stuck and the queue behind it blocked.

        The ``run_options`` column is cleared in the same statement, and its text
        is **carried into ``error``** — the reader's message names the fields that
        failed, never the values, so the column is the only copy of what the row
        held and clearing it alone would destroy it. The statement reads the
        column and writes both fields itself (SQL's own concatenation), so no
        other writer can slip between the read and the clear. Only a ``queued`` or
        ``running`` row is moved, so a run that finished is never rewritten by a
        caller that read a stale list. Returns whether a row moved.
        """
        with self._session() as session:
            result = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    # The states the fail move is legal from
                    # (``lifecycle.FAIL``): a run that finished is never rewritten
                    # by a caller that read a stale list.
                    entities.PipelineRun.status.in_(FAIL.sources),
                )
                .values(
                    status=FAIL.target,
                    ended_at=_now(),
                    error=error
                    + case(
                        (
                            or_(
                                entities.PipelineRun.run_options.is_(None),
                                entities.PipelineRun.run_options == "",
                            ),
                            "",
                        ),
                        else_=tr(KEPT_OPTIONS_LEAD) + entities.PipelineRun.run_options,
                    ),
                    run_options=None,
                )
            )
            return bool(result.rowcount)

    def get_run(self, run_id: int) -> PipelineRun | None:
        with self._session() as session:
            row = session.get(entities.PipelineRun, run_id)
            return self._run(row) if row is not None else None

    def list_runs(self, meeting_id: int) -> list[PipelineRun]:
        with self._session() as session:
            rows = session.scalars(
                select(entities.PipelineRun)
                .where(entities.PipelineRun.meeting_id == meeting_id)
                .order_by(entities.PipelineRun.id.desc())
            )
            return [self._run(row) for row in rows]

    # --- the node queue (one run at a time) -------------------------------- #
    def active_run_for_meeting(self, meeting_id: int) -> PipelineRun | None:
        """The meeting's newest run that is ``queued`` or ``running``, if any.

        This is the guard the run manager reads before it inserts — the check
        that refuses a second submission without writing a row — and *not* the
        rule: one active run per meeting is the table's own invariant, held by
        revision 0009's partial unique index, which is what stops the second
        writer when both read "no active run". Being derived from the registry
        rather than from process memory is what lets it survive a restart.
        """
        with self._session() as session:
            row = session.scalar(
                select(entities.PipelineRun)
                .where(
                    entities.PipelineRun.meeting_id == meeting_id,
                    entities.PipelineRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .order_by(entities.PipelineRun.id.desc())
                .limit(1)
            )
            return self._run(row) if row is not None else None

    def runs_with_status(
        self, *statuses: str, limit: int | None = None
    ) -> list[PipelineRun]:
        """Every run in one of ``statuses``, oldest first (the FIFO order).

        ``limit`` bounds the query to the **newest** ``limit`` runs — read and
        decoded in the database rather than in the caller — and still returns
        them oldest first, so a caller that only needs recent history (the
        history-based ETA) does not pay for the whole table.
        """
        if not statuses:
            return []
        stmt = select(entities.PipelineRun).where(
            entities.PipelineRun.status.in_(statuses)
        )
        if limit is None:
            stmt = stmt.order_by(entities.PipelineRun.id)
        else:
            stmt = stmt.order_by(entities.PipelineRun.id.desc()).limit(limit)
        with self._session() as session:
            runs = [self._run(row) for row in session.scalars(stmt)]
        return runs if limit is None else list(reversed(runs))

    def oldest_queued_run(self) -> PipelineRun | None:
        """The head of the node's FIFO: the oldest run still ``queued``."""
        runs = self.runs_with_status(QUEUED)
        return runs[0] if runs else None

    def finished_runs(
        self, *statuses: str, limit: int | None = None
    ) -> list[PipelineRun]:
        """Runs in ``statuses``, newest **finish** first.

        :meth:`runs_with_status` orders by id, which is *creation* order — and a
        run created later can finish earlier (the queue is FIFO, a cancel is
        not), so "the newest run" and "the run that finished most recently" are
        not the same row. Ranking a status view by ``ended_at`` is what "newest
        finished run" means there: the chip's own state and the history it links
        to have to agree.

        ``ended_at`` is written by every terminal transition, so the
        ``created_at`` fallback only covers a row written by something that did
        not set one; ``id`` breaks a tie between two runs that ended in the same
        second. ``limit`` bounds the query to the newest ``limit`` in that
        order, read and ordered in the database rather than in the caller.
        """
        if not statuses:
            return []
        stmt = (
            select(entities.PipelineRun)
            .where(entities.PipelineRun.status.in_(statuses))
            .order_by(
                func.coalesce(
                    entities.PipelineRun.ended_at, entities.PipelineRun.created_at
                ).desc(),
                entities.PipelineRun.id.desc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._session() as session:
            return [self._run(row) for row in session.scalars(stmt)]

    @audit.recorded("run.claim", audit.subject("run", "run_id"), conditional=True)
    def claim_run(self, run_id: int, *, actor: str, owner: str) -> PipelineRun | None:
        """Claim a ``queued`` run for ``owner``: the node's one write for ``running``.

        Returns the claimed row, or ``None`` when the claim is lost — either
        another process claimed this run first, or the node already has a
        ``running`` run (the queue's one-at-a-time rule, enforced by the same
        statement rather than by the read that chose the run).

        This is deliberately **one statement**. SQLite's write lock decides the
        winner *inside* it, so two claimants that both read ``queued`` cannot both
        write ``running``. It also means the write lock is taken before any row of
        this run is read: a claimant can never hold a read snapshot from before
        the other's commit, which is the upgrade SQLite refuses to wait for (it
        returns ``SQLITE_BUSY`` without consulting the busy handler). Contention
        therefore waits on the connection's busy timeout instead of failing, and
        a crude read-then-update — the shape this replaces — has no such promise.

        ``started_at`` and the first heartbeat are written here, together with the
        status: a claimed run is provably alive from the instant it is claimed.
        """
        at = _now()
        with self._session() as session:
            claimed = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    # The state the claim move is legal from (``lifecycle.CLAIM``).
                    entities.PipelineRun.status.in_(CLAIM.sources),
                    ~select(entities.PipelineRun.id)
                    .where(entities.PipelineRun.status == RUNNING)
                    .correlate(None)
                    .exists(),
                )
                .values(
                    status=CLAIM.target, started_at=at, owner=owner, heartbeat_at=at
                ),
                execution_options=_NO_SYNC,
            )
            if claimed.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    @audit.recorded("run.heartbeat", audit.subject("run", "run_id"), conditional=True)
    def heartbeat_run(self, run_id: int, *, actor: str, at: str | None = None) -> bool:
        """Refresh a running run's liveness heartbeat; ``False`` when it did not land.

        A ``False`` is not an error to act on: it means the row is not ``running``
        any more (a peer reaped it, believing the owner dead) or does not exist.
        The executing process keeps working either way — a pipeline has no
        cancellation contract — and its own terminal write is what the run finally
        says. The value returned is for the caller (and its tests) to see which of
        the two happened.

        A landed beat is also what protects a run whose reap is already in
        flight: :meth:`interrupt_run` compares the row against the heartbeat a
        reap decided from, so a beat written while that decision was being made
        is the owner saying it is alive, and the run is left alone.
        """
        at = at or _now()
        with self._session() as session:
            landed = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    entities.PipelineRun.status == RUNNING,
                )
                .values(heartbeat_at=at),
                execution_options=_NO_SYNC,
            )
            return landed.rowcount == 1

    @audit.recorded("run.interrupt", audit.subject("run", "run_id"), conditional=True)
    def interrupt_run(
        self,
        run_id: int,
        *,
        actor: str,
        observed: PipelineRun,
        ended_at: str,
        error: str,
        progress: dict,
    ) -> PipelineRun | None:
        """Move a ``running`` run whose owner is gone to ``interrupted``.

        The conditional counterpart of :meth:`claim_run`, and conditional for the
        same reason: a reaper decides from a **snapshot** — a heartbeat that had
        gone stale — and writes afterwards, so the row may not be the one it
        judged by the time it writes. ``observed`` is that snapshot, and the
        statement compares the row against the **ownership and heartbeat the
        decision was based on**, not just the status. Three things can move the row
        in between: the owner finishing (:meth:`update_run`), a peer's reap
        already landing (this same method, from another node), or — the case
        this compare exists for — the owner refreshing its heartbeat
        (:meth:`heartbeat_run`) because it was alive all along. Each moves the
        row before this write can, so each wins the compare.

        ``None`` means the compare lost: the row is not the one judged dead, so
        the caller has a **stale observation** on its hands rather than an
        interruption, and it leaves the row (and the meeting) alone. A run that
        reached its own end is not an orphan, whatever its heartbeat said a moment
        ago, and a run whose owner refreshed its heartbeat during the decision is
        one whose owner is provably alive.

        Kept as a Core expression (ADR-0030): the compare *is* the statement, and
        ``IS`` is how it is written — ``IS NULL`` for a snapshot that recorded no
        owner or heartbeat, ``IS ?`` for one that recorded them.
        """
        with self._session() as session:
            reaped = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    # The states the interrupt move is legal from
                    # (``lifecycle.INTERRUPT``).
                    entities.PipelineRun.status.in_(INTERRUPT.sources),
                    entities.PipelineRun.owner.is_(observed.owner),
                    entities.PipelineRun.heartbeat_at.is_(observed.heartbeat_at),
                )
                .values(
                    status=INTERRUPT.target,
                    ended_at=ended_at,
                    error=error,
                    progress=json.dumps(progress),
                ),
                execution_options=_NO_SYNC,
            )
            if reaped.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    @audit.recorded("run.stop", audit.subject("run", "run_id"), conditional=True)
    def stop_run(
        self, run_id: int, *, actor: str, ended_at: str, progress: dict
    ) -> PipelineRun | None:
        """Move a **queued** run to ``stopped``, before anyone claimed it (RUN-04).

        The counterpart of :meth:`claim_run` for a run that has not started:
        both are conditional on the state the move is legal from —
        :data:`~clear_record.service.lifecycle.STOP_QUEUED` and
        :data:`~clear_record.service.lifecycle.CLAIM` both start at ``queued`` —
        so a cancel and a claim cannot both win: whoever loses sees no row and
        acts on what it finds instead. Moving the row out of ``queued`` is what
        makes a cancellation stick: the drain never picks it up, the meeting's
        active-run guard lets go of it, and a restart has nothing to resurrect.

        ``None`` means the run was not ``queued`` any more (claimed or already
        terminal): the caller re-reads it rather than insisting.
        """
        with self._session() as session:
            stopped = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    # The state the queued stop move is legal from
                    # (``lifecycle.STOP_QUEUED``).
                    entities.PipelineRun.status.in_(STOP_QUEUED.sources),
                )
                .values(
                    status=STOP_QUEUED.target,
                    ended_at=ended_at,
                    progress=json.dumps(progress),
                ),
                execution_options=_NO_SYNC,
            )
            if stopped.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    @audit.recorded("run.cancel", audit.subject("run", "run_id"), conditional=True)
    def request_cancel(
        self, run_id: int, *, actor: str, at: str | None = None
    ) -> PipelineRun | None:
        """Record a cancel **request** on a ``running`` run (RUN-04).

        A running run belongs to whoever is executing it, and only that process
        may end it: its pipeline is mid-write in a workspace, so a second process
        flipping the row to a terminal status would record a stop that did not
        happen and let the work continue unheard. This writes the request; the
        owner honours it (it reads this column on every heartbeat), stops at its
        next safe boundary and writes ``stopped`` itself.

        The first request's timestamp is kept, so asking twice is idempotent
        rather than a rewrite. ``None`` means the run was not ``running`` any
        more — already terminal by the time the request arrived.
        """
        with self._session() as session:
            asked = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    entities.PipelineRun.status == RUNNING,
                )
                .values(
                    cancel_requested_at=func.coalesce(
                        entities.PipelineRun.cancel_requested_at, at or _now()
                    )
                ),
                execution_options=_NO_SYNC,
            )
            if asked.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    def cancel_requested(self, run_id: int) -> bool:
        """Whether a cancel has been requested for this run (RUN-04).

        Read by the executing owner's heartbeat, which is the only place a
        *request* can become an actual stop: cheap, one column, and only asked
        while a run is live.
        """
        with self._session() as session:
            asked_at = session.scalar(
                select(entities.PipelineRun.cancel_requested_at).where(
                    entities.PipelineRun.id == run_id
                )
            )
            return bool(asked_at)

    def queue_position(self, run_id: int) -> int:
        """A queued run's 1-based place in the FIFO (``0`` when not queued).

        Position ``1`` is next: nothing queued before it. The count is the
        registry's, so it is stable across a restart.
        """
        run = self.get_run(run_id)
        if run is None or run.status != QUEUED:
            return 0
        with self._session() as session:
            ahead = session.scalar(
                select(func.count())
                .select_from(entities.PipelineRun)
                .where(
                    entities.PipelineRun.status == QUEUED,
                    entities.PipelineRun.id < run_id,
                )
            )
            return int(ahead) + 1

    # --- the persisted event stream ---------------------------------------- #
    @audit.recorded("run.event", audit.subject("run", "run_id"))
    def add_run_event(self, run_id: int, event: JobEvent, *, actor: str) -> int:
        """Append one progress event to a run's durable stream; return its seq.

        The sequence is assigned atomically by the insert, so a run's chunk
        workers can report concurrently and the stream still has one order. That
        is why the insert stays one statement — the sequence is a subquery
        *inside* it, read under the same write lock that writes the row, not a
        ``MAX`` read by the caller and passed in.
        """
        payload = json.dumps(dataclasses.asdict(event), ensure_ascii=False)
        with self._session() as session:
            appended = session.execute(
                insert(entities.RunEvent).values(
                    run_id=run_id,
                    seq=(
                        select(func.coalesce(func.max(entities.RunEvent.seq), 0) + 1)
                        .where(entities.RunEvent.run_id == run_id)
                        .scalar_subquery()
                    ),
                    payload=payload,
                    created_at=_now(),
                )
            )
            row = session.get(entities.RunEvent, appended.inserted_primary_key[0])
            return int(row.seq)

    def list_run_events(self, run_id: int, after: int = 0) -> list[JobEvent]:
        """A run's persisted events with ``seq`` greater than ``after``, in order."""
        with self._session() as session:
            payloads = session.scalars(
                select(entities.RunEvent.payload)
                .where(
                    entities.RunEvent.run_id == run_id,
                    entities.RunEvent.seq > after,
                )
                .order_by(entities.RunEvent.seq)
            )
            return [self._event(json.loads(payload)) for payload in payloads]

    def latest_run_event(self, run_id: int) -> JobEvent | None:
        """A run's last persisted event, or ``None`` when it has none.

        The one-event read for a caller that wants a run's *current* stage and
        progress — the console's status page, which renders a live run from the
        registry alone (RUN-03) — rather than its whole stream: a transcribe
        stage persists one event per chunk, so reading them all to keep the last
        is work the reader does not need and cannot bound.
        """
        with self._session() as session:
            payload = session.scalar(
                select(entities.RunEvent.payload)
                .where(entities.RunEvent.run_id == run_id)
                .order_by(entities.RunEvent.seq.desc())
                .limit(1)
            )
            return self._event(json.loads(payload)) if payload is not None else None

    def count_run_events(self, run_id: int) -> int:
        with self._session() as session:
            count = session.scalar(
                select(func.count())
                .select_from(entities.RunEvent)
                .where(entities.RunEvent.run_id == run_id)
            )
            return int(count)

    @staticmethod
    def _event(payload: dict) -> JobEvent:
        """Rebuild a :class:`JobEvent`, ignoring any field a newer build added."""
        known = {field.name for field in dataclasses.fields(JobEvent)}
        return JobEvent(
            **{key: value for key, value in payload.items() if key in known}
        )

    # --- artifacts --------------------------------------------------------- #
    @audit.recorded("artifact.add", audit.subject("meeting", "meeting_id"))
    def add_artifact(
        self,
        meeting_id: int,
        *,
        actor: str,
        kind: str,
        path: str,
        run_id: int | None = None,
        sha256: str | None = None,
        bytes: int | None = None,
        produced_by: str = "pipeline",
        review_state: str = "final",
    ) -> Artifact:
        with self._session() as session:
            row = entities.Artifact(
                meeting_id=meeting_id,
                run_id=run_id,
                kind=kind,
                path=path,
                sha256=sha256,
                bytes=bytes,
                produced_by=produced_by,
                review_state=review_state,
                created_at=_now(),
            )
            session.add(row)
            session.flush()
            return self._artifact(row)

    def list_artifacts(self, meeting_id: int) -> list[Artifact]:
        with self._session() as session:
            rows = session.scalars(
                select(entities.Artifact)
                .where(entities.Artifact.meeting_id == meeting_id)
                .order_by(entities.Artifact.kind, entities.Artifact.id)
            )
            return [self._artifact(row) for row in rows]

    def latest_artifact(self, meeting_id: int, kind: str) -> Artifact | None:
        """The meeting's newest artifact of ``kind``, or ``None``.

        "Newest" is the highest id, which is the insertion order: a meeting's
        ``minutes`` artifact is the one most recently accepted, so a re-accept
        supersedes the earlier one without rewriting history.
        """
        with self._session() as session:
            row = session.scalar(
                select(entities.Artifact)
                .where(
                    entities.Artifact.meeting_id == meeting_id,
                    entities.Artifact.kind == kind,
                )
                .order_by(entities.Artifact.id.desc())
                .limit(1)
            )
            return self._artifact(row) if row is not None else None

    # --- archives ---------------------------------------------------------- #
    @audit.recorded("archive.add", audit.subject("meeting", "meeting_id"))
    def add_archive(
        self,
        meeting_id: int,
        project_id: int,
        *,
        actor: str,
        root_path: str,
        manifest_path: str,
        manifest_sha256: str,
    ) -> Archive:
        """Record an already-written archive directory.

        The files are the caller's job (see
        :func:`clear_record.service.archive.archive_meeting`); the registry only
        remembers where the copy is and how to seal it.
        """
        if self.meeting_by_id(meeting_id) is None:
            raise KeyError(meeting_id)
        with self._session() as session:
            row = entities.Archive(
                meeting_id=meeting_id,
                project_id=project_id,
                root_path=root_path,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                created_at=_now(),
            )
            session.add(row)
            session.flush()
            return self._archive(row)

    def list_archives(self, meeting_id: int) -> list[Archive]:
        with self._session() as session:
            rows = session.scalars(
                select(entities.Archive)
                .where(entities.Archive.meeting_id == meeting_id)
                .order_by(entities.Archive.id.desc())
            )
            return [self._archive(row) for row in rows]

    def get_archive(self, archive_id: int) -> Archive | None:
        with self._session() as session:
            row = session.get(entities.Archive, archive_id)
            return self._archive(row) if row is not None else None

    # --- row mapping ------------------------------------------------------- #
    #
    # Entities in, dataclasses out: every value that crosses the seam is built
    # here, inside the unit of work that read it, so no entity and no session
    # outlives the call that returned it.
    @staticmethod
    def _meeting_entity(
        session: Session, meeting_id: int
    ) -> tuple[entities.Meeting, str]:
        """The meeting row and its project's slug, or ``KeyError``."""
        row = session.execute(
            _meeting_query().where(entities.Meeting.id == meeting_id)
        ).first()
        if row is None:
            raise KeyError(meeting_id)
        meeting, project_slug = row
        return meeting, project_slug

    @staticmethod
    def _term_entity(
        session: Session, term_id: int
    ) -> tuple[entities.GlossaryTerm, str]:
        """The term row and its project's slug, or ``KeyError``."""
        row = session.execute(
            _term_query().where(entities.GlossaryTerm.id == term_id)
        ).first()
        if row is None:
            raise KeyError(term_id)
        term, project_slug = row
        return term, project_slug

    def _run_row(self, session: Session, run_id: int) -> PipelineRun:
        """A run by id, as the boundary type; ``KeyError`` when it is gone."""
        row = session.get(entities.PipelineRun, run_id)
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    @staticmethod
    def _project(row: entities.Project) -> Project:
        return Project(
            id=row.id,
            slug=row.slug,
            name=row.name,
            notes=row.notes,
            default_archive_root=row.default_archive_root,
            created_at=row.created_at,
        )

    @staticmethod
    def _term(row: entities.GlossaryTerm, project_slug: str) -> GlossaryTerm:
        return GlossaryTerm(
            id=row.id,
            project_id=row.project_id,
            project_slug=project_slug,
            term=row.term,
            reading=row.reading,
            aliases=row.aliases,
            definition=row.definition,
            status=row.status,
            added_by=row.added_by,
            notes=_split_retire_marker(row.notes)[1],
            created_at=row.created_at,
        )

    @staticmethod
    def _meeting(row: entities.Meeting, project_slug: str) -> Meeting:
        return Meeting(
            id=row.id,
            project_id=row.project_id,
            project_slug=project_slug,
            slug=row.slug,
            title=row.title,
            recorded_at=row.recorded_at,
            workspace_path=row.workspace_path,
            notes=row.notes,
            status=row.status,
            created_at=row.created_at,
        )

    @staticmethod
    def _recording_set(row: entities.RecordingSet) -> RecordingSet:
        return RecordingSet(
            id=row.id,
            meeting_id=row.meeting_id,
            paths=tuple(json.loads(row.paths)),
            created_at=row.created_at,
        )

    @staticmethod
    def _tape(row: entities.Tape) -> Tape:
        return Tape(
            id=row.id,
            meeting_id=row.meeting_id,
            path=row.path,
            sha256=row.sha256,
            bytes=row.bytes,
            created_at=row.created_at,
        )

    def _run(self, row: entities.PipelineRun) -> PipelineRun:
        """The boundary value for one run row, validating its stored options.

        The one mapper that is an instance method rather than a ``@staticmethod``:
        the read seam's warning is keyed by the registry a row belongs to, and the
        row itself does not carry that.
        """
        return PipelineRun(
            id=row.id,
            meeting_id=row.meeting_id,
            status=row.status,
            backend=row.backend,
            model=row.model,
            language=row.language,
            options=json.loads(row.options) if row.options else None,
            started_at=row.started_at,
            ended_at=row.ended_at,
            error=row.error,
            created_at=row.created_at,
            # The one row whose text is a declared shape rather than opaque meta:
            # a queued run is executed from it, so it is validated here, where the
            # registry hands a run on, and a row that no longer fits fails loudly
            # instead of arriving reduced (ADR-0030, `service.run_options`).
            run_options=read_run_options(
                row.id,
                row.run_options,
                meeting_id=row.meeting_id,
                registry=str(self.db_path),
            ),
            progress=json.loads(row.progress) if row.progress else None,
            origin=row.origin,
            owner=row.owner,
            heartbeat_at=row.heartbeat_at,
            resumes_run_id=row.resumes_run_id,
            cancel_requested_at=row.cancel_requested_at,
        )

    @staticmethod
    def _artifact(row: entities.Artifact) -> Artifact:
        return Artifact(
            id=row.id,
            meeting_id=row.meeting_id,
            run_id=row.run_id,
            kind=row.kind,
            path=row.path,
            sha256=row.sha256,
            bytes=row.bytes,
            produced_by=row.produced_by,
            review_state=row.review_state,
            created_at=row.created_at,
        )

    @staticmethod
    def _audit_event(row: entities.AuditEvent) -> AuditEvent:
        return AuditEvent(
            id=row.id,
            at=row.at,
            actor=row.actor,
            action=row.action,
            target=row.target,
            outcome=row.outcome,
        )

    @staticmethod
    def _console_session(row: entities.ConsoleSession) -> ConsoleSession | None:
        """The row as the auth gate reads it, or ``None`` when it is unreadable."""
        instants = (
            _parsed_instant(row.created_at),
            _parsed_instant(row.seen_at),
            _parsed_instant(row.idle_deadline),
            _parsed_instant(row.absolute_deadline),
        )
        if any(instant is None for instant in instants):
            return None
        created_at, seen_at, idle_deadline, absolute_deadline = instants
        assert created_at and seen_at and idle_deadline and absolute_deadline
        return ConsoleSession(
            created_at=created_at,
            seen_at=seen_at,
            idle_deadline=idle_deadline,
            absolute_deadline=absolute_deadline,
        )

    @staticmethod
    def _archive(row: entities.Archive) -> Archive:
        return Archive(
            id=row.id,
            meeting_id=row.meeting_id,
            project_id=row.project_id,
            root_path=row.root_path,
            manifest_path=row.manifest_path,
            manifest_sha256=row.manifest_sha256,
            created_at=row.created_at,
        )


__all__ = ["Registry", "RegistryLocked"]
