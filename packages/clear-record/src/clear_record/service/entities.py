"""The registry's tables, as SQLAlchemy entities (ADR-0030).

One class per table of the application's schema, and nothing else: the columns
the registry reads and writes, with the foreign keys and the uniqueness the
revisions give them. The schema's own bookkeeping tables — Alembic's
``alembic_version`` and the retired ladder's ``schema_version`` — are not
described here; they are Core tables in :mod:`clear_record.service.store`, read
once when a registry is opened. The schema stays Alembic's: these classes
*describe* the tables and never create them (``Base.metadata`` is deliberately
not handed to Alembic — ``migrations/env.py`` carries why), so a change here can
neither emit DDL nor move a revision.

Two things this module deliberately does not have:

- **Domain behaviour.** An entity is a persistence shape, not a domain type: no
  method, no validation, no derived value. The boundary types are the frozen
  dataclasses of :mod:`clear_record.service.models`, which the store builds out
  of these rows; no entity leaves the service layer.
- **Relationships.** A join is written out in the query that needs it (see
  ``store._meeting_query`` and ``store._term_query``) rather than declared as a
  relationship: an attribute access that fires its own query is one that can fire
  it after the session that read the row is gone — the failure a session that
  lives one operation makes likely for a registry the console reads and an MCP
  server writes.

The column types follow the revisions' DDL: ``TEXT`` is :class:`~sqlalchemy.Text`,
a row id is :class:`~sqlalchemy.Integer`, and a nullable column is a nullable
annotation. Two things of that DDL are not repeated here — its ``DEFAULT`` values
(so a default restated here would be an unused second statement of the same rule:
every insert in the store names the columns it writes) and its plain indexes
(they are the queries' performance detail, not a shape a query depends on). The
uniqueness the registry relies on *is* declared: a violation is what the store
reads as "already exists" — the table's ``UNIQUE`` constraints, and the partial
unique index revision 0009 puts over the active run of a meeting
(``pipeline_run_active_meeting``), which the schema states once for every writer
rather than only in the check that races.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from clear_record.service.lifecycle import active_run_predicate

#: The predicate the mapping *describes* for the index revision 0009 creates,
#: rendered by the lifecycle's own rule: ``sqlite_where`` wants literal text, and
#: this is :func:`~clear_record.service.lifecycle.active_run_predicate`'s
#: rendering of the states that rule accepts, so the mapping cannot disagree with
#: the guard that reads the same declaration. No DDL is emitted from here — the
#: index on disk is the revision's own statement, and it is pinned against this
#: rendering by ``tests/service/test_store.py`` — so this is the mapping's account
#: of the rule, not a second place that creates it.
_ACTIVE_PREDICATE = active_run_predicate()


class Base(DeclarativeBase):
    """The registry's declarative base: table shapes, no DDL of its own."""


class Project(Base):
    """``project`` — a durable container for meetings, a glossary and archives."""

    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text)
    default_archive_root: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class GlossaryTerm(Base):
    """``glossary_term`` — one term of one project's glossary."""

    __tablename__ = "glossary_term"
    __table_args__ = (UniqueConstraint("project_id", "term"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    term: Mapped[str] = mapped_column(Text)
    reading: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class Meeting(Base):
    """``meeting`` — one recording session inside a project."""

    __tablename__ = "meeting"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    slug: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[str | None] = mapped_column(Text)
    workspace_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class RecordingSet(Base):
    """``recording_set`` — the tapes chosen for a meeting (the newest wins)."""

    __tablename__ = "recording_set"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE")
    )
    paths: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class Tape(Base):
    """``tape`` — an uploaded tape and the copy's integrity facts (ADR-0024)."""

    __tablename__ = "tape"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)


class PipelineRun(Base):
    """``pipeline_run`` — one execution of the pipeline against a tape set."""

    __tablename__ = "pipeline_run"
    #: One active run per meeting, stated where every writer must obey it.
    #: Partial, so it constrains the *active* run and not the meeting's history: a
    #: ``done``/``failed``/``stopped``/``interrupted`` run holds nothing, and the
    #: meeting starts again. This describes the index revision 0009 creates, and
    #: its predicate is the one-active-run rule as
    #: :func:`~clear_record.service.lifecycle.active_run_predicate` renders it,
    #: rather than the pair spelled again — the revision has to spell it (a
    #: revision states its own DDL, and nothing emits this declaration's:
    #: ``migrations/env.py`` sets ``target_metadata=None``), and
    #: ``tests/service/test_store.py`` compares the predicate the database built
    #: with the declaration.
    __table_args__ = (
        Index(
            "pipeline_run_active_meeting",
            "meeting_id",
            unique=True,
            sqlite_where=text(_ACTIVE_PREDICATE),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text)
    backend: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    options: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    run_options: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[str | None] = mapped_column(Text)
    #: A plain column, not a foreign key: revision 0008 added it without one, and
    #: its reference is checked by the store rather than by the table.
    resumes_run_id: Mapped[int | None] = mapped_column(Integer)
    cancel_requested_at: Mapped[str | None] = mapped_column(Text)


class RunEvent(Base):
    """``run_event`` — one progress event of a run's durable stream."""

    __tablename__ = "run_event"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class Artifact(Base):
    """``artifact`` — a file a run or an agent produced, with its checksum."""

    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE")
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    produced_by: Mapped[str] = mapped_column(Text)
    review_state: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class Archive(Base):
    """``archive`` — an immutable, checksummed copy of a meeting's tapes."""

    __tablename__ = "archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE")
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    root_path: Mapped[str] = mapped_column(Text)
    manifest_path: Mapped[str] = mapped_column(Text)
    manifest_sha256: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class AuditEvent(Base):
    """``audit_event`` — the row one mutating service call appends (ADR-0033).

    A **conditional** write that matched no row, and a **key miss**, append
    nothing, because nothing happened.

    ``at`` is when the call happened, ``actor`` who made it (a word of
    :data:`~clear_record.service.lifecycle.ACTORS`), ``action`` the verb,
    ``target`` what it touched, and ``outcome`` how it ended.

    ``target`` carries the service's own **address** for what was touched
    (``project:demo``, ``run:12``, ``draft:9f2c…``) and no foreign key, so a row
    outlives what it names: a deleted tape's audit row still says who deleted it.
    No relationship either, like every other entity here.

    The table is **append-only for row DML**, not only by convention: revision
    0010 puts triggers on it that refuse every ``UPDATE``, every ``DELETE`` and
    every ``REPLACE``/``INSERT`` of an id the table already holds (SQLite fires
    ``BEFORE INSERT`` before ``REPLACE``'s conflict path deletes the row). That is
    the file-level guard, and it is scoped to those statements: an **append** is
    what the table is for, and other statements that reach the file — ``ALTER
    TABLE``, ``DROP TRIGGER``, a schema edit through ``PRAGMA writable_schema`` —
    are not refused by the triggers, so the record is append-only, not
    tamper-proof against a process that rewrites the schema. The registry's engine
    also turns ``recursive_triggers`` on for every connection it makes, which is
    defence in depth: it lets the delete half of a ``REPLACE`` reach the delete
    trigger too, where the insert-side guard above sees only the insert half. See
    :meth:`~clear_record.service.store.Registry.record_audit`.
    """

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)


class ConsoleCredential(Base):
    """``console_credential`` — the one credential that gates the console.

    **One row**, enforced by the table rather than by the code that writes it:
    ``CHECK (id = 1)`` means a second credential cannot exist, so "there is one
    human and one credential" (ADR-0033) is a property of the file. ``encoded`` is
    the salted hash :func:`~clear_record.service.auth.hash_password` produces
    (``scrypt$n$r$p$salt$hash``) — never a password, and never logged — and
    ``updated_at`` is when it was last set or replaced.

    No interface column, no username, no ``added_by``: who set it is an audit
    question, answered by the ``credential.set`` row, and an identity stored
    beside a shared secret is one more thing to leak.
    """

    __tablename__ = "console_credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encoded: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class ConsoleSession(Base):
    """``console_session`` — one signed-in browser's server-side session.

    The primary key is the **digest** of the cookie's token
    (:func:`~clear_record.service.auth.token_digest`), so a registry leak hands
    out no cookie that works and the process holds no session state at all: every
    request re-reads this row, which is what makes sign-out and revoke-all take
    effect on the next request with no restart. Four instants and nothing else —
    there is no user to name, and the client's address is deliberately not kept
    (one local human, and an address is a fact to leak for no question it
    answers).

    The two deadlines are absolute instants, computed when the session was opened
    or last touched: ``idle_deadline`` moves forward with each accepted request,
    ``absolute_deadline`` never moves.
    """

    __tablename__ = "console_session"

    token_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text)
    seen_at: Mapped[str] = mapped_column(Text)
    idle_deadline: Mapped[str] = mapped_column(Text)
    absolute_deadline: Mapped[str] = mapped_column(Text)


__all__ = [
    "Archive",
    "Artifact",
    "AuditEvent",
    "Base",
    "ConsoleCredential",
    "ConsoleSession",
    "GlossaryTerm",
    "Meeting",
    "PipelineRun",
    "Project",
    "RecordingSet",
    "RunEvent",
    "Tape",
]
