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
                from app import aktualisierung

                with session_factory()() as db:
                    aktualisierung.automatisch(db)
            except Exception:
                log.exception("Update-Prüfung fehlgeschlagen")
            try:
                with session_factory()() as db:
                    benachrichtigung.pruefen(db)
            except Exception:
                log.exception("Benachrichtigungen fehlgeschlagen")
            try:
                from app import umzug

                with session_factory()() as db:
                    umzug.aufraeumen(db)  # 0.4.8: Umzugsdateien nach 24 h, liegen gebliebene Aufträge
            except Exception:
                log.exception("Umzug: Aufräumen fehlgeschlagen")
            try:
                from app import woerterbuch

                woerterbuch.automatisch()  # einmal laden; danach nichts mehr zu tun
            except Exception:
                log.exception("Wortlisten fehlgeschlagen")
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
        from app.aufbewahrung import festlegen

        festlegen(db)  # Aufbewahrung der Aufnahmen: neue Server bis zur Freigabe, bestehende wie bisher
    from app.extern import arbeitsprozess_starten as extern_starten

    if get_settings().worker_art:
        from app.verwaltung.lokaler_worker import eingebauten_worker_koppeln

        try:
            with session_factory()() as db:
                datei = eingebauten_worker_koppeln(db)
            log.info("Eingebauter Worker (%s): Schlüssel liegt in %s", get_settings().worker_art, datei)
        except OSError:
            log.exception("Eingebauter Worker: Schlüssel ließ sich nicht schreiben")
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
    from app.koerper import Grenze

    # Obergrenze für jede Anfrage (auch Multipart und chunked), größer als jedes erlaubte Teil
    app.add_middleware(Grenze, max_bytes=max(settings.max_body_bytes, settings.chunk_size_bytes + 1024 * 1024))
    class TalewardCORS(CORSMiddleware):
        """Erlaubte Herkünfte ändern sich zur Laufzeit (Verwaltung: zentrale Web-App, öffentliche Adresse)."""

        def is_allowed_origin(self, origin: str) -> bool:
            from app.webapp import erlaubte_herkuenfte

            return origin in erlaubte_herkuenfte()

    app.add_middleware(
        TalewardCORS,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Chunk-SHA256", "Accept-Language", "X-Taleward-App", "Range"],
        expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges"],
        max_age=600,
    )
    install_error_handlers(app)

    api = APIRouter(prefix=API_PREFIX)

    @api.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    from app.routers import anmeldung

    from app.routers import charaktere

    from app.routers import umzug as umzug_router

    from app.routers import plaene

    for r in (auth.router, anmeldung.router, voice_profile.router, charaktere.router, umzug_router.router, campaigns.router, sessions.router, pruefung.router, uploads.router, entries.router, miteinander.router, unterlagen.router, plaene.router):
        api.include_router(r)
    app.include_router(api)
    app.include_router(worker.router)
    app.include_router(anmeldung.seiten)
    from app.routers import downloads, webapp_seiten

    app.include_router(downloads.router)
    app.include_router(webapp_seiten.router)
    from app.verwaltung.router import einbinden

    einbinden(app)
    return app


app = create_app()
