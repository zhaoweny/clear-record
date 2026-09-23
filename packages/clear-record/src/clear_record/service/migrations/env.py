"""Alembic's environment for the registry's schema history.

The registry migrates when it opens (ADR-0030): :mod:`clear_record.service.store`
builds the Alembic configuration in code and runs the upgrade itself, so no
``alembic.ini`` needs to sit beside an installed wheel. This file is that
run's hook — and the same one the developer CLI uses:

    alembic -x db=/path/to/registry.sqlite3 upgrade head

``target_metadata`` is ``None``, and that is a **decision, not a deferral**
(ADR-0030): the revisions are the schema's single source of truth and stay
hand-written, while :mod:`clear_record.service.entities` maps the same tables as
a *query* shape that never emits DDL. Its ``Base.metadata`` must not be handed to
``context.configure`` — wiring it would make ``--autogenerate`` diff against a
mapping that is not the schema's authority. Measured against today's mapping,
that diff is nine ``alter_column`` calls, one per table's ``id`` (SQLite reflects
an ``INTEGER PRIMARY KEY`` as nullable; the mapping declares it not), seven
``drop_index``, ten ``drop_constraint``/``create_foreign_key`` pairs, and — on a
registry the retired ladder wrote — ``drop_table('schema_version')``, the table
``store._migrate`` still reads. The chain's shape changes nothing about that:
the released baseline and the deltas on top of it describe the tables the
retired ladder's steps did, so the diff above is what *autogenerate* would want
to change, not what this build has.
"""

from __future__ import annotations

from alembic import command, context
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, pool
from sqlalchemy.engine import URL

config = context.config

#: The id a revision gets on an **empty** chain. It is a fallback and nothing
#: more: the chain ships with a revision (``0006``, the released baseline), so
#: the lowest id it actually reaches is the baseline's, and this value is reached
#: only by a chain someone has emptied. Revision ids in this chain are decimal
#: because the baseline's id is the ladder number a released install records
#: (``store._RELEASED_BASELINES``), and because ``store._LADDER_VERSION`` is the
#: number a pre-Alembic build compares *itself* with — a hex id would make the
#: first unexpressible and the second unrecoverable, which is the landmine this
#: pins away.
_FIRST_REVISION = 1


def _database_url() -> URL:
    """The registry to migrate.

    The application hands in the URL object it opened the registry by; the
    developer CLI passes ``-x db=``. Both become a URL object here rather than a
    string, because a path holding a ``?`` does not survive a round trip through
    URL text — it reads back as a query, and the wrong file gets migrated.
    """
    supplied = config.attributes.get("database_url")
    if supplied is not None:
        return supplied
    path = context.get_x_argument(as_dictionary=True).get("db")
    if path is None:
        raise RuntimeError(
            "no registry to migrate: pass `-x db=/path/to/registry.sqlite3`"
        )
    return URL.create("sqlite", database=path)


def _writing_a_revision() -> bool:
    """Whether this run of ``env.py`` only generates a revision file.

    ``alembic.ini`` sets ``revision_environment = true`` so that
    :func:`_next_revision_id` — which is registered by the ``configure`` call
    below — also applies to ``alembic revision``. That mode wants the *hooks*,
    not a registry: it has none to reach and none to migrate, so the registry
    connection is not made — the hooks run against a scratch in-memory bind — and
    the run ends after the directives were offered.

    The CLI marks the subcommand by *callable* in ``config.cmd_opts`` (the
    namespace's ``cmd`` is ``(callable, positional, kwargs)``, not a name), and a
    programmatic
    ``command.revision`` run carries no ``cmd_opts`` at all — the caller that
    does that either supplies a connection or wants the hooks.
    """
    options = getattr(config, "cmd_opts", None)
    marked = getattr(options, "cmd", None)
    if not isinstance(marked, tuple) or not marked:
        return False
    return marked[0] is command.revision


def _foreign_keys_on(dbapi_connection: object, _record: object) -> None:
    """Enforce foreign keys on the migration's own connection.

    SQLite sets this per connection and defaults it **off**, so the application
    engine turns it on for every connection it makes
    (``store._engine``). A migration that ran on a connection of its own would
    otherwise build the schema under different rules from the processes that use
    it — invisible today, and wrong the moment a revision rebuilds a table (the
    create-copy-drop-rename pattern, where the copy's constraints are what the
    DDL says they are).
    """
    dbapi_connection.execute("PRAGMA foreign_keys = ON")  # type: ignore[attr-defined]


def _next_revision_id(*_args: object, **_kwargs: object) -> None:
    """Give a new revision a **decimal** id, the way the chain's ids are written.

    ``alembic revision`` (the workflow ``alembic.ini`` documents) otherwise names
    the file with ``${up_revision}`` = a hex uuid, and the chain's ids have to
    stay decimal for the **first** of them: the baseline's id is the ladder
    number the released line recorded its schema as
    (``store._RELEASED_BASELINES``), because that number is what places a
    released registry at that revision, and a hex id there would make it
    unexpressible. The id is the newest revision's number plus one
    (:data:`_FIRST_REVISION` on an empty chain), so a new revision also stays
    comparable with the number a pre-Alembic build wrote
    (``store._LADDER_VERSION``) and with the file name and the
    ``alembic_version`` row beside it.

    Wired as ``process_revision_directives``, which Alembic calls with the
    pending directives, so the pin holds for ``--autogenerate`` too. An explicit
    ``--rev-id`` is overridden with everything else: the numbering is the
    chain's, and one hex id in it is what the pin exists to prevent.
    """
    directives = _args[2] if len(_args) > 2 else _kwargs.get("directives")
    if not directives:
        return
    heads = [head for head in ScriptDirectory.from_config(config).get_heads()]
    numeric = [int(head) for head in heads if str(head).isdigit()]
    next_id = f"{max(numeric) + 1 if numeric else _FIRST_REVISION:04d}"
    for directive in directives:  # type: ignore[attr-defined]
        directive.rev_id = next_id


def _configure(connection: object | None = None, **extra: object) -> None:
    """One ``context.configure``, so both runs carry the same hooks."""
    context.configure(
        connection=connection,
        target_metadata=None,
        process_revision_directives=_next_revision_id,
        **extra,
    )


def run_migrations_offline() -> None:
    """Render the SQL for one registry instead of executing it (``alembic --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=_next_revision_id,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Migrate the registry on one connection.

    The application hands the connection in (``config.attributes``), so the whole
    migration — the version tables' read, the stamp, the revisions — runs inside
    the single write lock ``store.Registry._migrate`` holds, and two surfaces
    starting at once cannot interleave. The developer CLI passes none, and this
    run then takes a connection of its own: not pooled, not outliving the run,
    with the same foreign-key rule every application connection carries.
    """
    supplied = config.attributes.get("connection")
    if supplied is not None:
        # Inside the caller's transaction and lock: no engine, no commit here.
        _configure(supplied)
        with context.begin_transaction():
            context.run_migrations()
        return
    if _writing_a_revision():
        # The hooks are what this mode wants, and Alembic reaches them through
        # ``run_migrations``, which reads the version table — so the context
        # needs a bind. A scratch in-memory one is that bind: the revision's
        # parent comes from the script directory, not from a registry, so
        # nothing of the user's is read and nothing is written. `_database_url`
        # would have nothing to resolve here, and must not be asked.
        scratch = create_engine("sqlite://", poolclass=pool.NullPool)
        try:
            with scratch.connect() as connection:
                _configure(connection)
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            scratch.dispose()
        return
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    event.listen(connectable, "connect", _foreign_keys_on)
    with connectable.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
