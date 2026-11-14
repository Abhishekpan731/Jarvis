"""Alembic environment. Migrations run synchronously against a psycopg-free URL.

asyncpg cannot be used by Alembic's migration runner, so the URL is rewritten
to psycopg for the duration of a migration and nowhere else.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    import os

    url = os.environ.get("DATABASE_URL_SYNC") or config.get_main_option("sqlalchemy.url")
    return url.replace("postgresql+asyncpg", "postgresql+psycopg")


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
