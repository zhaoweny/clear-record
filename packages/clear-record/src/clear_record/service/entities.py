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
(every insert in the store names every column it writes, so a default would be a
second, unused statement of the same rule: a row written without one is refused
by the table's ``NOT NULL`` rather than silently defaulted) and its plain indexes
(they are the queries' performance detail, not a shape a query depends on). The
uniqueness the registry relies on *is* declared: a violation is what the store
reads as "already exists".
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    #: A plain column, not a foreign key: the revision added it as one, and the
    #: reference is checked by the store rather than by the table.
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


__all__ = [
    "Archive",
    "Artifact",
    "Base",
    "GlossaryTerm",
    "Meeting",
    "PipelineRun",
    "Project",
    "RecordingSet",
    "RunEvent",
    "Tape",
]
