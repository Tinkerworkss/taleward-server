"""Datenbankanbindung (SQLite im WAL-Modus, damit API und später der Worker parallel zugreifen können)."""
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Speichert naiv in UTC, liefert immer zeitzonenbewusste UTC-Zeitpunkte zurück."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Zeitpunkt ohne Zeitzone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None
_server_id: str | None = None


def _set_sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})
        event.listen(_engine, "connect", _set_sqlite_pragmas)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Für Tests: Verbindung neu aufbauen (nach Änderung der Einstellungen)."""
    global _engine, _SessionLocal, _server_id
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    _server_id = None


def session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Bringt die Datenbank per Alembic auf den neuesten Stand."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    get_engine()  # legt data_dir an
    command.upgrade(cfg, "head")


def server_id() -> str:
    """Dauerhafte ID dieses Servers (JWT-aud). Wird beim ersten Aufruf erzeugt und in der DB gespeichert."""
    global _server_id
    if _server_id is None:
        import uuid

        from app.models import ServerMeta

        with session_factory()() as db:
            row = db.get(ServerMeta, "server_id")
            if row is None:
                row = ServerMeta(key="server_id", value=f"chronik-{uuid.uuid4()}")
                db.add(row)
                db.commit()
            _server_id = row.value
    return _server_id
