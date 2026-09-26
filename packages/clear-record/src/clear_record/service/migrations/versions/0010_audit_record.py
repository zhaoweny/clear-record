"""Revision 0010 — the audit record (a delta on the released baseline).

Revision ID: 0010
Revises: 0009

Who called the service, what they touched, and how it ended: one **append-only**
row per mutating service call — ``(at, actor, action, target, outcome)`` — as
ADR-0033 decides. ``actor`` is the word a transport supplied about itself
(``console``, ``api``, ``mcp``, ``cli``, ``tray``, or ``queue`` for the node's own
queue), never a string a caller chose; ``target`` is the service's own address for
what was touched (``meeting:Kickoff``, ``tape:12``, ``project:demo``), deliberately
not a foreign key, so a row outlives what it names.

**Append-only is the schema's, not a convention's.** The service has no statement
that updates or deletes a row (:meth:`~clear_record.service.store.Registry.record_audit`
is the only way one is written at all), and these two triggers make that hold
against anything else that reaches the file: a mutating statement against
``audit_event`` is refused by the database. A record of who did what that its own
subject could rewrite is not a record.

The triggers are recreated (``DROP TRIGGER IF EXISTS`` then ``CREATE``) rather than assumed (``DROP TRIGGER IF EXISTS`` then
``CREATE``) so the revision converges when it is re-run over an already-built
schema — the repair path ``store._pending_stamp`` takes after an open was killed
part-way through the chain. A trigger is the only thing this revision leaves that
``CREATE TABLE IF NOT EXISTS`` cannot make idempotent by itself.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

#: This revision's DDL: the record's table. ``IF NOT EXISTS`` for the same reason
#: the baseline's tables carry it — a re-run over a built schema is a no-op.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS audit_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    outcome   TEXT NOT NULL
)""",
)

#: The statements that make the table append-only, as ``(name, DDL)``. The
#: message is the reader's own sentence; ``RAISE(ABORT, …)`` is what turns the
#: attempt into the driver's error rather than a silent no-op.
#:
#: Three, because ``REPLACE`` is a third way to rewrite a row and it is refused
#: twice over, at the two levels that matter:
#:
#: - :data:`audit_event_no_replace` is the **file-level** guarantee: SQLite fires
#:   ``BEFORE INSERT`` *before* ``REPLACE``'s conflict path deletes the
#:   conflicting row, so the guard sees the id still in the table and refuses.
#:   That holds whatever a connection's settings are, which is what lets the
#:   entity say nothing that reaches the file can rewrite a row.
#: - ``PRAGMA recursive_triggers = ON`` (set on every connection the registry's
#:   engine makes, ``store._engine``) is the **connection-level** one: it makes
#:   the delete inside a ``REPLACE`` fire :data:`audit_event_no_delete` as well,
#:   so the two statements that rewrite the file are each refused by the trigger
#:   that names what they do.
#:
#: Neither is redundant: the trigger needs no cooperation from a connection, and
#: the pragma covers the conflict path for every connection the registry makes.
_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "audit_event_no_update",
        """CREATE TRIGGER audit_event_no_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'the audit record is append-only: a row is written, never rewritten');
END""",
    ),
    (
        "audit_event_no_delete",
        """CREATE TRIGGER audit_event_no_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'the audit record is append-only: a row is written, never removed');
END""",
    ),
    (
        "audit_event_no_replace",
        """CREATE TRIGGER audit_event_no_replace
BEFORE INSERT ON audit_event
WHEN EXISTS (SELECT 1 FROM audit_event WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'the audit record is append-only: a row is written, never replaced');
END""",
    ),
)


def upgrade() -> None:
    """Create the audit record and the triggers that keep it append-only."""
    for statement in _DDL:
        op.execute(statement)
    for name, ddl in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
        op.execute(ddl)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
