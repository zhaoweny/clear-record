"""Alembic's environment for the registry's schema history.

The registry migrates when it opens (ADR-0030): :mod:`clear_record.service.store`
builds the Alembic configuration in code and runs the upgrade itself, so no
``alembic.ini`` needs to sit beside an installed wheel. This file is that
run's hook — and the same one the developer CLI uses:

    alembic -x db=/path/to/registry.sqlite3 upgrade head

``target_metadata`` is ``None``: the tables are hand-written statements in the
revisions, and the mapping that will name them is later work in this lane. This
change adds no table, column or index.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

config = context.config


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


def run_migrations_offline() -> None:
    """Render the SQL for one registry instead of executing it (``alembic --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Migrate the registry on a connection of this run's own.

    The connection is not pooled and does not outlive the run: the registry's
    operations keep their own connection discipline (one connection per
    operation), and the session lifecycle the mapping will bring belongs to the
    change that brings it.
    """
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
