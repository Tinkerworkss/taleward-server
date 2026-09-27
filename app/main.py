"""Einstiegspunkt: uvicorn app.main:app"""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import run_migrations
from app.errors import install_error_handlers
from app.routers import auth, campaigns, entries, miteinander, pruefung, sessions, unterlagen, uploads, voice_profile, worker

log = logging.getLogger("chronik")
API_PREFIX = "/api/v1"
DEFAULT_SECRET = "bitte-aendern"


def check_settings() -> None:
    s = get_settings()
    if s.jwt_secret == DEFAULT_SECRET or len(s.jwt_secret) < 32:  # kann nur passieren, wenn data/ nicht schreibbar
        raise RuntimeError("Kein Geheimnis für Anmeldungen: JWT_SECRET setzen oder den Datenordner schreibbar machen.")


def _wartung_starten() -> threading.Event | None:
    """Regelmäßige Wartung: abgelaufene Leases zurückholen, Audio nach Frist löschen, verwaiste Uploads."""
    takt = get_settings().maintenance_interval_seconds
    if takt <= 0:
        return None
    stop = threading.Event()

    def schleife():
        from app import benachrichtigung, modellablage, sicherung
        from app.db import session_factory
        from app.queue import sweep

        while True:  # erste Runde sofort beim Start (abgelaufene Leases nach einem Neustart)
            try:
                with session_factory()() as db:
                    ergebnis = sweep(db)
                    from app import einmal

                    einmal.aufraeumen(db)
                if any(ergebnis.values()):
                    log.info("Wartung: %s", ergebnis)
            except Exception:  # Wartung darf den Server nie stoppen
                log.exception("Wartung fehlgeschlagen")
            try:
                with session_factory()() as db:
                    try:
                        if sicherung.automatisch(db) is not None:
                            benachrichtigung.sicherung_gelungen(db)
                    except Exception as e:
                        log.exception("Automatische Sicherung fehlgeschlagen")
                        benachrichtigung.sicherung_fehlgeschlagen(db, f"{e.__class__.__name__}: {e}")
            except Exception:
                log.exception("Automatische Sicherung fehlgeschlagen")
            try:
                with session_factory()() as db:
                    modellablage.automatisch(db)
            except Exception:
                log.exception("Modellablage fehlgeschlagen")
            try:
                with session_factory()() as db:
                    benachrichtigung.pruefen(db)
            except Exception:
                log.exception("Benachrichtigungen fehlgeschlagen")
            if stop.wait(takt):
                break

    threading.Thread(target=schleife, name="wartung", daemon=True).start()
    return stop


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_settings()
    run_migrations()
    log.info("Datenbank bereit unter %s", get_settings().data_dir)
    from app.db import session_factory
    from app.zusammenfassung import arbeitsprozess_starten

    from app.einrichtung import beim_start

    with session_factory()() as db:
        beim_start(db)  # noch kein Verwalter: Einrichtungscode ins Protokoll
    from app.extern import arbeitsprozess_starten as extern_starten

    stops = [_wartung_starten(), arbeitsprozess_starten(session_factory()), extern_starten(session_factory())]
    if get_settings().local_worker_autostart:  # in Tests aus
        from app.verwaltung.lokaler_worker import KNECHT

        try:
            with session_factory()() as db:
                KNECHT.autostart(db)
        except Exception:
            log.exception("Lokaler Worker konnte nicht starten")
    yield
    for stop in stops:
        if stop:
            stop.set()
    from app.verwaltung.lokaler_worker import KNECHT

    KNECHT.beenden()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Session-Chronik Server",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
    )
    # App-Version prüfen (426) – vor CORS eingehängt, damit auch die 426-Antwort CORS-Kopfzeilen bekommt
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.versionen import pruefen

    app.add_middleware(BaseHTTPMiddleware, dispatch=pruefen)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Chunk-SHA256", "Accept-Language", "X-Taleward-App"],
        max_age=600,
    )
    install_error_handlers(app)

    api = APIRouter(prefix=API_PREFIX)

    @api.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    from app.routers import anmeldung

    for r in (auth.router, anmeldung.router, voice_profile.router, campaigns.router, sessions.router, pruefung.router, uploads.router, entries.router, miteinander.router, unterlagen.router):
        api.include_router(r)
    app.include_router(api)
    app.include_router(worker.router)
    app.include_router(anmeldung.seiten)
    from app.verwaltung.router import einbinden

    einbinden(app)
    return app


app = create_app()
