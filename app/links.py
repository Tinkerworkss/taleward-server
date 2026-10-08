"""Links an Kampagne, Bibel-Einträgen und Szenen des Kapitelplans (Schnittstelle 0.4.13).

Ein Link ist nur ein Verweis nach draußen (virtueller Spieltisch, Discord, Karte, Musik). Der Server ruft die Adresse
nie auf und bettet nichts ein. Er prüft nur: http:// oder https://, ein Host, keine Zugangsdaten in der Adresse.

Links fließen nie in Kapitel, Vorschläge, Schreibhilfe oder Prüfungen – die KI-Wege bauen ihre Texte aus einzelnen
Feldern und kennen das Feld nicht (Test in tests/test_links_0413.py).

Gespeichert wird als JSON-Text [{id, label, url, shared}] in der jeweiligen Zeile.
"""
import json
import re
from urllib.parse import urlsplit

from app import errors
from app.schemas import UUID_MUSTER

MAX_KAMPAGNE = 20
MAX_EINTRAG = 3
MAX_SZENE = 3


def adresse_ok(url: str) -> bool:
    if not url or len(url) > 2000 or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    try:
        teile = urlsplit(url)
        host = teile.hostname
    except ValueError:
        return False
    if teile.scheme.lower() not in ("http", "https") or not host:
        return False
    # Zugangsdaten (user:pass@ oder user@) gehören nicht in einen geteilten Link
    return "@" not in teile.netloc


def pruefen(links) -> list[dict]:
    """Eingabe aus der Schnittstelle (schemas.Link) prüfen → Liste zum Speichern. Fehler → 400 invalid_input."""
    aus, ids = [], set()
    for link in links or []:
        label = link.label.strip()
        url = link.url.strip()
        if not label:
            raise errors.bad_request("invalid_input", "invalid_input.link_label")
        if not adresse_ok(url):
            raise errors.bad_request("invalid_input", "invalid_input.link_url")
        lid = link.id.lower()
        if lid in ids:
            raise errors.bad_request("invalid_input", "invalid_input.link_id")
        ids.add(lid)
        aus.append({"id": lid, "label": label, "url": url, "shared": bool(link.shared)})
    return aus


def bereinigen(roh, hoechstens: int) -> list[dict]:
    """Links aus einer Umzugsdatei: Ungültiges fällt still heraus, statt den ganzen Umzug abzubrechen."""
    aus, ids = [], set()
    for x in roh or []:
        if not isinstance(x, dict):
            continue
        lid, label, url = x.get("id"), x.get("label"), x.get("url")
        if not (isinstance(lid, str) and re.match(UUID_MUSTER, lid) and isinstance(label, str)
                and isinstance(url, str)):
            continue
        label, url, lid = label.strip()[:60], url.strip(), lid.lower()
        if not label or not adresse_ok(url) or lid in ids:
            continue
        ids.add(lid)
        aus.append({"id": lid, "label": label, "url": url, "shared": x.get("shared") is True})
        if len(aus) >= hoechstens:
            break
    return aus


def lesen(text: str | None, nur_geteilt: bool = False) -> list[dict]:
    try:
        links = json.loads(text or "[]")
    except ValueError:
        return []
    if not isinstance(links, list):
        return []
    return [x for x in links if isinstance(x, dict) and (not nur_geteilt or x.get("shared") is True)]


def schreiben(links: list[dict]) -> str | None:
    return json.dumps(links, ensure_ascii=False) if links else None
