"""Revision 0006 — the released baseline: the schema `v0.2.0` shipped.

Revision ID: 0006
Revises: nothing (an empty registry)

The history begins at the schema a **released** install has, and carries only
the deltas from it to today's head. `v0.2.0` (and its release candidates) is the
release this baseline is — it is the only release that ever shipped a registry:
`v0.1.x` predates the registry entirely, so it has nothing to place and nothing
to carry. That release recorded its schema as ladder version **6**, and its
version table is the only version state a released build writes; the id of this
revision is that number, which is what places an existing released registry here
(``store._RELEASED_BASELINES``, ``store._pending_stamp``) without re-running
anything it already has.

Why the chain starts here rather than at the retired ladder's first step. The
chain this one replaces replayed the ladder's eight DDL steps one apiece
(``0001``–``0008``, with the per-meeting index as ``0009`` on top), so a registry
could be placed at whatever ladder number it had reached. That was the *shape*
the ladder needed and the compressed baseline replaces: five of those revisions
(``0001``–``0005``) are gone — a released registry never stood at one of them,
and a development build's registry that did is not carried (the tip
wipe-and-reinstall policy, ``docs/releasing.md``) — while ``0007`` and ``0008``
survive as the deltas they are from a released registry's viewpoint. What is left
of the ladder in this chain is one thing only — the number a released install
stands at, which is this revision's id.

The DDL below is the ladder's steps 1–6 as written, with the two steps that
added a column folded into the tables that carry them: ``notes`` ends ``meeting``
(step 4) and ``run_options`` ends ``pipeline_run`` (step 6), each in the position
``ALTER TABLE … ADD COLUMN`` gave it, so a registry this revision builds holds
the same tables, columns, orders, constraints and indexes as a released one.
Nothing else changed with the folding: this revision **creates**, it never
alters, so running it over a schema that is already built is a no-op for every
table it finds — which is what lets an open killed part-way through the migration
replay from the base (``store._pending_stamp``).

The ladder's own ``schema_version`` table is deliberately **not** created here,
exactly as the revision that adopted the ladder's first step left it out (that
step created the table; adopting the step into this chain meant not recreating
it): the row records the ladder history a registry *migrated from the ladder*
carries, and a fresh registry has none — its version state is ``alembic_version``
alone.
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = None
branch_labels = None
depends_on = None

#: This revision's DDL: the released baseline, ladder step by ladder step, the
#: two column-adding steps folded in where their column belongs.
_DDL: tuple[str, ...] = (
    # v1 — the app-owned project and glossary tables.
    """CREATE TABLE IF NOT EXISTS project (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    notes                TEXT NOT NULL DEFAULT '',
    default_archive_root TEXT,
    created_at           TEXT NOT NULL
);""",
    """CREATE TABLE IF NOT EXISTS glossary_term (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    term        TEXT NOT NULL,
    reading     TEXT,
    aliases     TEXT,
    definition  TEXT,
    status      TEXT NOT NULL DEFAULT 'candidate',
    added_by    TEXT NOT NULL DEFAULT 'human',
    notes       TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, term)
);""",
    """CREATE INDEX IF NOT EXISTS glossary_term_project ON glossary_term (project_id);""",
    # v2 — the meeting, run and artifact spine.
    """CREATE TABLE IF NOT EXISTS meeting (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug           TEXT NOT NULL,
    title          TEXT NOT NULL,
    recorded_at    TEXT,
    workspace_path TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    created_at     TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, slug)
);""",
    """CREATE TABLE IF NOT EXISTS recording_set (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    paths      TEXT NOT NULL,
    created_at TEXT NOT NULL
);""",
    """CREATE TABLE IF NOT EXISTS pipeline_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id    INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    backend       TEXT,
    model         TEXT,
    language      TEXT,
    options       TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    error         TEXT,
    progress      TEXT,
    created_at    TEXT NOT NULL,
    run_options   TEXT
);""",
    """CREATE TABLE IF NOT EXISTS artifact (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    run_id       INTEGER REFERENCES pipeline_run(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT,
    bytes        INTEGER,
    produced_by  TEXT NOT NULL DEFAULT 'pipeline',
    review_state TEXT NOT NULL DEFAULT 'final',
    created_at   TEXT NOT NULL
);""",
    """CREATE INDEX IF NOT EXISTS meeting_project ON meeting (project_id);""",
    """CREATE INDEX IF NOT EXISTS pipeline_run_meeting ON pipeline_run (meeting_id);""",
    """CREATE INDEX IF NOT EXISTS artifact_meeting ON artifact (meeting_id);""",
    # v3 — the archive spine.
    """CREATE TABLE IF NOT EXISTS archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    root_path       TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at      TEXT NOT NULL
);""",
    """CREATE INDEX IF NOT EXISTS archive_meeting ON archive (meeting_id);""",
    # v5 — the managed workspace's tape.
    """CREATE TABLE IF NOT EXISTS tape (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);""",
    """CREATE INDEX IF NOT EXISTS tape_meeting ON tape (meeting_id);""",
    # v6 — durable run state and the node queue.
    """CREATE TABLE IF NOT EXISTS run_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);""",
    """CREATE INDEX IF NOT EXISTS run_event_run ON run_event (run_id);""",
)


def upgrade() -> None:
    """Apply this revision's DDL: the schema the released line shipped."""
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "the registry's schema is forward-only: a revision is upgraded, never unwound"
    )
