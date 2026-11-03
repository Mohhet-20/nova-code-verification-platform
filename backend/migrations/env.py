from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Tests and tools may pre-set the URL on the Alembic config; otherwise use NOVA_DATABASE_URL.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from app.db.session import build_engine

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = build_engine(section["sqlalchemy.url"]) if section["sqlalchemy.url"] else \
        engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # SQLite "batch" migrations rebuild tables; with foreign keys enforced, dropping the old
            # table would silently cascade-delete child rows. Disable enforcement for the migration
            # (must happen outside a transaction), then verify integrity afterwards.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # end the implicit transaction so migrations manage their own
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
        if is_sqlite:
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            if violations:
                raise RuntimeError(f"Migration left {len(violations)} foreign key violation(s)")
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
