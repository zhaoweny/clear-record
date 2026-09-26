"""Revision 0011 — the console credential and its sessions (a delta on 0010).

Revision ID: 0011
Revises: 0010

The auth gate's two **operational** tables (ADR-0033), beside the registry's
project data and its audit record:

- ``console_credential`` — **one** row, the salted hash the console's first run
  sets and the rescue command replaces. ``CHECK (id = 1)`` is the schema's own
  statement that one install has one credential: a second row is refused by the
  file, not by whichever statement remembered to look. Nothing here is a
  password — the value is ``scrypt$n$r$p$salt$hash``, the salt fresh per install
  — and there is deliberately no username beside it: who set it is the
  ``credential.set`` audit row's question, and a name stored next to a shared
  secret is one more thing to leak.
- ``console_session`` — one row per signed-in browser, keyed by the **digest** of
  the cookie's token. Four instants and nothing else: the process holds no
  session state, so every request re-reads this table, which is what makes
  sign-out and revoke-all effective on the very next request with no restart.
  ``idle_deadline`` moves forward with each accepted request and
  ``absolute_deadline`` never moves; both are absolute instants, so the verdict
  is a comparison rather than a recomputation of a policy that may have changed.

Neither table is audited, and both are excluded from the audit record's rule the
same way the setup marker is: they are not history of the operator's data. The
one mutation of security state that *is* an attribution question — who set the
credential — appends its ``credential.set`` row through
:meth:`~clear_record.service.store.Registry.record_audit`, like any other
mutation of durable state.

Both statements carry ``IF NOT EXISTS`` for the reason the baseline's do: a
re-run over a schema that already has these tables is a no-op, which is what the
open-path repair after a half-applied chain depends on. No index is created: the
tables hold one row and a handful of rows, and every read is by primary key.
"""

from __future__ import annotations

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None

#: This revision's DDL. The credential's ``CHECK`` is the interesting half: it is
#: what makes "one credential" hold for a writer that ignores this build's code.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS console_credential (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    encoded    TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS console_session (
    token_digest      TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    seen_at           TEXT NOT NULL,
    idle_deadline     TEXT NOT NULL,
    absolute_deadline TEXT NOT NULL
)""",
)


def upgrade() -> None:
    """Create the credential table and the session table."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
