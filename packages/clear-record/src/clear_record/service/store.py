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
  (ADR-0030). The revisions live in :mod:`clear_record.service.migrations`; a
  registry of any older version is moved forward on open, and one recorded at a
  revision this build does not carry fails loudly rather than being silently
  misread. The migration is forward-only — a revision is upgraded, never
  unwound.
- **Slugs are stable, human-readable ids.** A project is addressable by slug in
  the API, the GUI and any agent context; ids stay internal.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
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
    create_engine,
    event,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from clear_record.core.events import JobEvent
from clear_record.core.paths import registry_path
from clear_record.service import entities, tapestore
from clear_record.service.models import (
    MEETING_STATUSES,
    RUN_ORIGINS,
    RUN_STATUSES,
    TERM_AUTHORS,
    TERM_STATUSES,
    Archive,
    Artifact,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
    Tape,
)

# --- the schema's history, owned by Alembic (ADR-0030) --------------------- #
#
# The revisions in `clear_record.service.migrations` are this registry's schema.
# They are the retired ladder's steps, adopted one for one and verbatim: the
# ladder's version numbers became the revision ids, its DDL became the revision
# bodies, and no table, column, index or constraint changed with the adoption.

#: Where the schema's history lives. Named as a package resource rather than a
#: path, so that one value resolves both in a checkout (the member is installed
#: editable) and in an installed wheel; the repository's `alembic.ini` carries
#: the same value for the developer CLI.
_SCRIPT_LOCATION = "clear_record.service:migrations"

#: The retired ladder's version table, and what it is for now.
#:
#: Only a registry that carried it *before* this build has one: the ladder
#: created it and revision 0001 deliberately does not, so a registry this build
#: creates has no such record. Where it exists, the row is read once to place the
#: stamp and then left at the head revision's number — a build from before
#: Alembic reads *this* row, the head's schema is the ladder's own last one, and
#: the number is what lets that build treat the ladder as already applied
#: instead of re-running it. Where it does not exist, such a build runs the
#: ladder instead and stops at the first step that is not idempotent, v4's
#: ``ALTER TABLE meeting ADD COLUMN notes`` ("duplicate column name"), its
#: earlier steps adding nothing but this table. A revision that changes a shape
#: the ladder describes, rather than adding to it, is what would revisit this.
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


def _engine(db_path: Path) -> Engine:
    """The engine one registry reads and writes through.

    SQLite keeps the connection's state, not just the database's: foreign-key
    enforcement is off by default and is set per connection, so it is set on
    every connection the engine makes — the guard the store's hand-written
    connection used to carry, now the engine's own.

    The pool is :class:`~sqlalchemy.pool.NullPool`, so a connection is made for
    one unit of work and closed with it: the discipline the hand-written
    connection had (connections are cheap; correctness beats pooling), and the
    one that keeps a connection from being handed to a second thread, since the
    console, the web app's threadpool and the MCP server read one registry. The
    busy timeout is the driver's default, unchanged.
    """
    engine = create_engine(
        URL.create("sqlite", database=str(db_path)), poolclass=NullPool
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

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


def _revision_history(config: Config) -> tuple[frozenset[str], str]:
    """Every revision this build carries, and the newest of them."""
    script = ScriptDirectory.from_config(config)
    return (
        frozenset(entry.revision for entry in script.walk_revisions()),
        ", ".join(script.get_heads()),
    )


def _pending_stamp(conn: Connection, known: frozenset[str], head: str) -> str | None:
    """The revision a registry that predates Alembic stands at, if any.

    Two shapes arrive from before this build: a registry Alembic has already
    stamped (it stands at the revision it records) and one the hand-rolled
    ladder wrote (its ``schema_version`` names that revision directly, because
    the revision ids *are* the ladder's version numbers, adopted along with its
    DDL). Either way, a registry that records something this build does not
    carry — a newer release's revision — raises here, before any revision runs:
    half-understanding a schema is worse than refusing to open it.
    """
    if _has_table(conn, _ALEMBIC_VERSION_TABLE):
        for (revision,) in conn.execute(select(_ALEMBIC_VERSION.c.version_num)):
            if revision not in known:
                raise RuntimeError(
                    f"registry schema revision {revision} is not one this build "
                    f"carries (its newest is {head}); upgrade clear-record"
                )
        return None
    if not _has_table(conn, _LEGACY_VERSION_TABLE):
        return None  # no registry here yet: every revision runs from the base
    row = conn.execute(select(_LEGACY_VERSION.c.version)).first()
    version = int(row[0]) if row is not None else 0
    if version == 0:
        return None  # the ladder's own "nothing ran yet"
    revision = f"{version:04d}"
    if revision not in known:
        raise RuntimeError(
            f"registry schema version {version} is newer than this build "
            f"carries (its newest revision is {head}); upgrade clear-record"
        )
    return revision


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


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


def _term_query() -> Select[tuple[entities.GlossaryTerm, str]]:
    """A glossary term row joined to its project's slug."""
    return select(entities.GlossaryTerm, entities.Project.slug).join(
        entities.Project, entities.Project.id == entities.GlossaryTerm.project_id
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
        registry, RUN-02) wait for a writer instead of raising ``database is
        locked``. The one shape SQLite refuses to wait for is a write that has
        to upgrade a read transaction; pysqlite keeps the hand-written
        connection's promise here — it begins the transaction when the first
        write executes, not when the session reads, so a read followed by a
        write still reaches the write lock holding no read lock, and the write
        waits on the busy timeout rather than failing.

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

        An existing registry of any older version moves forward here: this is
        the auto-migration the hand-rolled ladder used to do, now Alembic's. A
        registry recording a revision this build does not carry is refused
        first (see :func:`_pending_stamp`) — before any revision runs, and
        before anything reads it as its own.
        """
        config = _alembic_config(self.db_path)
        known, head = _revision_history(config)
        with self._engine.connect() as conn:
            legacy = _has_table(conn, _LEGACY_VERSION_TABLE)
            stamp = _pending_stamp(conn, known, head)
        if stamp is not None:
            # A registry the hand-rolled ladder wrote: it already stands at
            # that revision, so record where it is and let the upgrade below
            # run only the revisions it is missing.
            command.stamp(config, stamp)
        command.upgrade(config, "head")
        if legacy:
            # Level the ladder's row with the head — it is what an older build
            # reads (see `_LEGACY_VERSION_TABLE`), and the head revision's
            # number is the ladder's own last version.
            with self._engine.connect() as conn:
                conn.execute(update(_LEGACY_VERSION).values(version=int(head)))
                conn.commit()

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

    def create_project(
        self,
        name: str,
        notes: str = "",
        default_archive_root: str | None = None,
        slug: str | None = None,
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

    def update_project(
        self,
        slug: str,
        *,
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
    def add_term(
        self,
        project_slug: str,
        term: str,
        *,
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

    def update_term(
        self,
        term_id: int,
        *,
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
                setattr(row, key, value)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ValueError("a term with that spelling already exists") from exc
            return self._term(row, project_slug)

    def delete_term(self, term_id: int) -> None:
        with self._session() as session:
            term = session.get(entities.GlossaryTerm, term_id)
            if term is None:
                raise KeyError(term_id)
            session.delete(term)

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

    def create_meeting(
        self,
        project_slug: str,
        title: str,
        *,
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

    def set_meeting_status(self, meeting_id: int, status: str) -> Meeting:
        if status not in MEETING_STATUSES:
            raise ValueError(
                f"status must be one of {MEETING_STATUSES}, got {status!r}"
            )
        with self._session() as session:
            row, project_slug = self._meeting_entity(session, meeting_id)
            row.status = status
            return self._meeting(row, project_slug)

    def set_meeting_workspace(self, meeting_id: int, workspace_path: str) -> Meeting:
        with self._session() as session:
            row, project_slug = self._meeting_entity(session, meeting_id)
            row.workspace_path = workspace_path
            return self._meeting(row, project_slug)

    def update_meeting(
        self,
        meeting_id: int,
        *,
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
    def set_recording_set(
        self, meeting_id: int, paths: list[str] | tuple[str, ...]
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

    # --- uploaded tapes ---------------------------------------------------- #
    def register_tape(
        self,
        meeting_id: int,
        *,
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

    def forget_tape(self, tape_id: int) -> Tape:
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
    def create_run(
        self,
        meeting_id: int,
        *,
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
        recorded so a queued run is picked back up after a restart. ``origin`` is
        the surface that started it, one of :data:`RUN_ORIGINS` (RUN-02); it is
        ``None`` only for a caller that is not a start path (a seeded row).

        ``resumes_run_id`` links this run to the run it continues (RUN-04). The
        reference is checked here rather than left to the reader: a link to a run
        that does not exist, or to a run of another meeting, would be a lie the
        registry itself could see.
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
                status="queued",
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

    def update_run(
        self,
        run_id: int,
        *,
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

        ``status='done'`` also **clears** ``error`` (RUN-02): a run that reached
        its own successful end reports no error, whatever a reaper wrote on it
        while wrongly believing the owner dead — the reason would otherwise sit on a
        finished run and be shown as a failure by the console and the API. There
        is no caller that wants both, so the clear wins over a passed ``error``.
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
        if status == "done":
            fields["error"] = None
        with self._session() as session:
            row = session.get(entities.PipelineRun, run_id)
            if row is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                setattr(row, key, value)
            return self._run(row)

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

        This is the persisted form of the "one run per meeting" dedupe: it is
        derived from the registry, so it survives a restart where the in-memory
        guard did not.
        """
        with self._session() as session:
            row = session.scalar(
                select(entities.PipelineRun)
                .where(
                    entities.PipelineRun.meeting_id == meeting_id,
                    entities.PipelineRun.status.in_(("queued", "running")),
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
        runs = self.runs_with_status("queued")
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

    def claim_run(self, run_id: int, *, owner: str) -> PipelineRun | None:
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
                    entities.PipelineRun.status == "queued",
                    ~select(entities.PipelineRun.id)
                    .where(entities.PipelineRun.status == "running")
                    .correlate(None)
                    .exists(),
                )
                .values(status="running", started_at=at, owner=owner, heartbeat_at=at),
                execution_options=_NO_SYNC,
            )
            if claimed.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    def heartbeat_run(self, run_id: int, *, at: str | None = None) -> bool:
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
                    entities.PipelineRun.status == "running",
                )
                .values(heartbeat_at=at),
                execution_options=_NO_SYNC,
            )
            return landed.rowcount == 1

    def interrupt_run(
        self,
        run_id: int,
        *,
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
                    entities.PipelineRun.status == "running",
                    entities.PipelineRun.owner.is_(observed.owner),
                    entities.PipelineRun.heartbeat_at.is_(observed.heartbeat_at),
                )
                .values(
                    status="interrupted",
                    ended_at=ended_at,
                    error=error,
                    progress=json.dumps(progress),
                ),
                execution_options=_NO_SYNC,
            )
            if reaped.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    def stop_run(
        self, run_id: int, *, ended_at: str, progress: dict
    ) -> PipelineRun | None:
        """Move a **queued** run to ``stopped``, before anyone claimed it (RUN-04).

        The counterpart of :meth:`claim_run` for a run that has not started:
        both are conditional on ``status = 'queued'``, so a cancel and a claim
        cannot both win — whoever loses sees no row and acts on what it finds
        instead. Moving the row out of ``queued`` is what makes a cancellation
        stick: the drain never picks it up, the meeting's active-run guard lets go
        of it, and a restart has nothing to resurrect.

        ``None`` means the run was not ``queued`` any more (claimed or already
        terminal): the caller re-reads it rather than insisting.
        """
        with self._session() as session:
            stopped = session.execute(
                update(entities.PipelineRun)
                .where(
                    entities.PipelineRun.id == run_id,
                    entities.PipelineRun.status == "queued",
                )
                .values(
                    status="stopped",
                    ended_at=ended_at,
                    progress=json.dumps(progress),
                ),
                execution_options=_NO_SYNC,
            )
            if stopped.rowcount == 0:
                return None
            return self._run_row(session, run_id)

    def request_cancel(
        self, run_id: int, *, at: str | None = None
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
                    entities.PipelineRun.status == "running",
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
        if run is None or run.status != "queued":
            return 0
        with self._session() as session:
            ahead = session.scalar(
                select(func.count())
                .select_from(entities.PipelineRun)
                .where(
                    entities.PipelineRun.status == "queued",
                    entities.PipelineRun.id < run_id,
                )
            )
            return int(ahead) + 1

    # --- the persisted event stream ---------------------------------------- #
    def add_run_event(self, run_id: int, event: JobEvent) -> int:
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
    def add_artifact(
        self,
        meeting_id: int,
        *,
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
    def add_archive(
        self,
        meeting_id: int,
        project_id: int,
        *,
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
            notes=row.notes,
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

    @staticmethod
    def _run(row: entities.PipelineRun) -> PipelineRun:
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
            run_options=json.loads(row.run_options) if row.run_options else None,
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


__all__ = ["Registry"]
