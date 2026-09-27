"""Revision 0012 — the machine tokens a script presents (a delta on 0011).

Revision ID: 0012
Revises: 0011

One more **operational** table beside the credential and the sessions (ADR-0033):
``machine_token``, where the operator's labelled bearer tokens live. A token is
minted in the console, shown **once**, and stored here as the SHA-256 digest of
its plaintext — never the plaintext, so the list can be rendered, read and
backed up without carrying a usable credential.

Three things the table states that the code around it must not have to:

- ``label`` is ``UNIQUE``: a label names one token, which is what makes the
  console's revoke action, the ``token:<label>`` audit target and a script's own
  note about which credential it holds mean one row each.
- ``token_digest`` is ``UNIQUE``: one row per token, and the look-up a request
  makes is that unique index rather than a scan. The **primary key** is a small
  integer ``id``, unlike the session table's digest-keyed rows, because the
  console's list needs a stable key for a row's revoke action that is not the
  hash of a secret.
- ``last_used_at`` is **nullable** on purpose: a token minted and never presented
  must say "never", not borrow its mint time.

The table carries no deadline and no session column: a token has no clocks and is
not a session — it authenticates a machine request until it is revoked, and the
revoke is a delete (the ``token.revoke`` audit row is what outlives it). It sits
outside the audit record's append-only rule for that reason, exactly as the
session rows do; the two writes that *are* attribution questions — who minted a
token, and who revoked it — go through
:meth:`~clear_record.service.store.Registry.record_audit` like any other
credential change.

The statement carries ``IF NOT EXISTS`` for the reason the earlier revisions' do:
a re-run over a schema that already has the table is a no-op, which is what the
open-path repair after a half-applied chain depends on. No extra index is
created: the digest's ``UNIQUE`` already indexes the one look-up a request makes,
and the list is a handful of rows read whole.
"""

from __future__ import annotations

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

#: This revision's DDL. The two ``UNIQUE`` constraints are the interesting half:
#: they are what makes "a label names one token" and "one row per token" hold for
#: a writer that ignores this build's code.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS machine_token (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL UNIQUE,
    token_digest TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
)""",
)


def upgrade() -> None:
    """Create the machine token table."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
