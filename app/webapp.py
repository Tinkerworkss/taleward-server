"""Web-Fassung der App (Schnittstelle 0.4.2): CORS für die zentrale Web-App und die eigene Adresse, erlaubte Rückwege
nach dem Anmelden mit einem Dienst (returnTo).

- Zentrale Web-App: CENTRAL_WEB_ORIGIN (Standard https://app.taleward.org, an der Wurzel), in der Verwaltung
  abschaltbar (server_meta web.zentral = "aus"). Bis 0.4.50 lag sie unter https://taleward.org/app/; diese alte
  Herkunft bleibt übergangsweise erlaubt (ALTE_ZENTRALE), bis die Website dauerhaft umleitet.
- Pfad: Die zentrale Web-App liegt an der Wurzel (/#/auth), die Web-App eines Servers und die alte zentrale unter /app/.
- Eigene Herkunft: aus der öffentlichen Adresse (PUBLIC_URL bzw. Einstellungen).
- Dazu die festen Herkünfte aus CORS_ORIGINS (App im Emulator, Entwicklung).
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import get_settings
from app.einstellungen import angaben, meta_lesen

_cache: dict = {"zeit": 0.0, "herkuenfte": frozenset()}

ALTE_ZENTRALE = "https://taleward.org"  # Übergang: nach dem Umzug der Website (301 auf app.taleward.org) entfernen
NEUE_ZENTRALE = "https://app.taleward.org"


def herkunft(url: str | None) -> str | None:
    if not url:
        return None
    t = urlparse(url.strip())
    if t.scheme not in ("http", "https") or not t.hostname:
        return None
    standard = {"http": 80, "https": 443}[t.scheme]
    port = f":{t.port}" if t.port and t.port != standard else ""
    return f"{t.scheme}://{t.hostname.lower()}{port}"


def zentral_erlaubt(db: Session) -> bool:
    return bool(get_settings().central_web_origin) and meta_lesen(db, "web.zentral") != "aus"


def konfigurierte_zentrale() -> str | None:
    """CENTRAL_WEB_ORIGIN; Installationen, die noch ausdrücklich die alte Adresse gesetzt haben, heben wir mit."""
    z = herkunft(get_settings().central_web_origin)
    return NEUE_ZENTRALE if z == ALTE_ZENTRALE else z


def zentrale_herkunft(db: Session) -> str | None:
    return konfigurierte_zentrale() if zentral_erlaubt(db) else None


def zentral_pfad(zentral: str) -> str:
    """Wo die zentrale Web-App liegt: an der Wurzel – nur die alte Adresse hatte /app/."""
    return "/app" if zentral == ALTE_ZENTRALE else ""


def herkuenfte(db: Session) -> frozenset[str]:
    werte = set(get_settings().cors_origin_list)
    eigene = herkunft(angaben(db).public_url)
    if eigene:
        werte.add(eigene)
    zentral = zentrale_herkunft(db)
    if zentral:
        werte.add(zentral)
        if zentral == NEUE_ZENTRALE:
            werte.add(ALTE_ZENTRALE)  # Übergang, bis die Website umleitet
    werte |= set(zusaetzliche(db))
    return frozenset(werte)


def zusaetzliche(db: Session) -> list[str]:
    """In der Verwaltung eingetragene weitere Herkünfte (eine je Zeile)."""
    return [h for h in (herkunft(z) for z in (meta_lesen(db, "web.herkuenfte") or "").splitlines()) if h]


def browser_link(db: Session, basis: str, einladung: str) -> str | None:
    """„Im Browser öffnen“: die Web-App dieses Servers, sonst die zentrale (falls erlaubt)."""
    from urllib.parse import quote

    from app import aktualisierung

    if aktualisierung.web_ordner(db) is not None:
        web, pfad = basis.rstrip("/"), "/app"
    else:
        web = zentrale_herkunft(db)
        pfad = zentral_pfad(web) if web else ""
    if not web:
        return None
    return f"{web}{pfad}/#/verbinden?invite=" + quote(einladung, safe="")


def erlaubte_herkuenfte() -> frozenset[str]:
    """Für die CORS-Prüfung bei jeder Anfrage – 30 s zwischengespeichert."""
    if time.monotonic() - _cache["zeit"] > 30:
        from app.db import session_factory

        with session_factory()() as db:
            _cache["herkuenfte"] = herkuenfte(db)
        _cache["zeit"] = time.monotonic()
    return _cache["herkuenfte"]


def vergessen() -> None:
    _cache["zeit"] = 0.0


def rueckwege(db: Session, basis: str) -> set[str]:
    """Erlaubte returnTo-Werte: <eigene Adresse>/app/#/auth und die zentrale Web-App (an der Wurzel). Nur feste
    Ziele, kein offener Umleiter."""
    ziele = {basis.rstrip("/") + "/app/#/auth"}
    zentral = zentrale_herkunft(db)
    if zentral:
        ziele.add(zentral + zentral_pfad(zentral) + "/#/auth")
        if zentral == NEUE_ZENTRALE:
            ziele.add(ALTE_ZENTRALE + "/app/#/auth")  # Übergang, bis die Website umleitet
    return ziele


def mit_parametern(ziel: str, query: str) -> str:
    """Parameter wörtlich anhängen – auch hinter #/auth (die Web-App liest sie dort)."""
    return f"{ziel}?{query}"
