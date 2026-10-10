"""Modellauswahl für die Cloud-API (0.4.61): Die Verwaltung zeigt Modelle zur Auswahl statt eines freien Felds.

Quelle: bekannte Modelle des Anbieters (app/cloudanbieter.py) und die Liste, die der Anbieter für den hinterlegten
Schlüssel meldet (GET /models). Die Liste wird je Adresse in server_meta gemerkt – beim ersten Aufruf der Seite mit
Schlüssel und bei „Verbindung prüfen“ neu geholt. Modelle, die erkennbar nicht für Text gedacht sind (Einbettung,
Texterkennung, Sprache, Bilder), fallen heraus.
"""
from __future__ import annotations

import json
import re

import httpx
from sqlalchemy.orm import Session

from app.einstellungen import meta_lesen, meta_schreiben

K_LISTE = "llm.modelle"
HOLEN = True  # Tests schalten das Holen beim Anbieter ab
_NICHT_TEXT = re.compile(r"embed|ocr|moderation|whisper|tts|transcri|voxtral|audio|image|dall-e|realtime|rerank|"
                         r"guard|search|vision-only|speech", re.I)


def _gespeichert(db: Session, url: str) -> list[str]:
    try:
        d = json.loads(meta_lesen(db, K_LISTE) or "{}")
    except ValueError:
        return []
    return [m for m in d.get("liste") or [] if isinstance(m, str)] if d.get("url") == url else []


def merken(db: Session, url: str, liste: list[str] | None) -> None:
    if liste:
        meta_schreiben(db, K_LISTE, json.dumps({"url": url, "liste": [m for m in liste if not _NICHT_TEXT.search(m)]}))


def holen(db: Session, k) -> None:
    """Liste beim Anbieter holen, wenn für diese Adresse noch keine da ist (ein kurzer Aufruf, Fehler egal)."""
    from app.sprachmodell import OpenAIKlient, SprachmodellFehler

    if not HOLEN or k.art != "api" or not k.api_key or _gespeichert(db, k.api_url):
        return
    try:
        liste = OpenAIKlient(k.api_url, k.api_key, k.api_modell,
                             client=httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))).modelle()
    except SprachmodellFehler:
        return
    if liste:
        merken(db, k.api_url, liste)
        db.commit()


def schluessel_abgelehnt(url: str, key: str) -> bool:
    """0.4.77: Lehnt der Anbieter einen neu eingetragenen Schlüssel ab (401/403 bei GET /models)? Browser setzen in
    Passwortfelder gern ein gespeichertes Passwort ein; ohne diese Probe überschrieb das beim Speichern den gültigen
    Schlüssel. Nicht erreichbar oder keine Liste: gilt nicht als abgelehnt (offline einrichten bleibt möglich)."""
    from app.sprachmodell import OpenAIKlient, SprachmodellFehler

    if not HOLEN or not url or not key:
        return False
    try:
        OpenAIKlient(url, key, "", client=httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))).modelle()
    except SprachmodellFehler:
        return True
    return False


def bekannte(anbieter) -> list[str]:
    if anbieter is None:
        return []
    return list(dict.fromkeys([anbieter.modell, *( [anbieter.modell_vorschlaege] if anbieter.modell_vorschlaege
                                                   else []), *anbieter.modelle]))


def auswahl(db: Session, k) -> list[str]:
    """Modelle für die Auswahl: Empfehlung und bekannte zuerst, dann die Liste des Anbieters, dazu die gewählten."""
    vorn = bekannte(k.anbieter)
    rest = sorted(m for m in _gespeichert(db, k.api_url) if m not in vorn)
    gewaehlt = [m for m in (k.api_modell, k.api_modell_vorschlaege, getattr(k, "api_modell_notizen", "")) if m]
    return list(dict.fromkeys([*vorn, *rest, *gewaehlt]))


def erlaubt(db: Session, url: str, anbieter, modell: str) -> bool:
    """Gehört das Modell zur Auswahl (bekannt oder vom Anbieter gemeldet)? Ohne bekannte Liste: jedes."""
    moeglich = set(bekannte(anbieter)) | set(_gespeichert(db, url))
    return not moeglich or modell in moeglich
