from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401 – registriert alle Tabellen
from app.config import get_settings
from app.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        # Tabellen werden bei SQLite neu aufgebaut – ohne Fremdschlüsselprüfung, sonst löscht das Entfernen der
        # alten Tabelle abhängige Zeilen (ON DELETE CASCADE).
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()  # sonst hält SQLAlchemy eine offene Transaktion, und die Migration wird verworfen
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
