"""Sicherungen: vollständig (Datenbank, Bilder, SL-Unterlagen), automatisch jede Nacht, mit Wiederherstellen.

Nicht enthalten: Aufnahmen in Bearbeitung und Hörproben (werden ohnehin gelöscht, sobald sie verarbeitet sind),
Arbeitsordner der Worker. Eine Sicherung ist eine ZIP-Datei mit manifest.json.

Einstellungen (server_meta): sicherung.auto (an|aus), sicherung.tage (wie viele Tage aufbewahren),
sicherung.ordner (zusätzlicher Ablageort, z. B. eine eingebundene Storage Box).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings

log = logging.getLogger("sicherung")
BERLIN = ZoneInfo("Europe/Berlin")
NAME = re.compile(r"^taleward-sicherung-\d{8}-\d{6}(-[a-z]+)?\.zip$")
ORDNER_IM_ZIP = ("bilder", "unterlagen")
STANDARD_TAGE = 14


def ablage() -> Path:
    return get_settings().data_dir / "sicherungen"


@dataclass
class Eintrag:
    name: str
    groesse: int
    zeit: datetime


def liste() -> list[Eintrag]:
    ordner = ablage()
    if not ordner.exists():
        return []
    out = [Eintrag(p.name, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))
           for p in ordner.iterdir() if NAME.match(p.name)]
    return sorted(out, key=lambda e: e.zeit, reverse=True)


def pfad(name: str) -> Path | None:
    """Nur echte Sicherungsdateien aus der Ablage (kein Pfad von außen)."""
    if not NAME.match(name or ""):
        return None
    p = ablage() / name
    return p if p.is_file() else None


def einstellungen(db) -> dict:
    from app.einstellungen import meta_lesen

    try:
        tage = int(meta_lesen(db, "sicherung.tage") or STANDARD_TAGE)
    except ValueError:
        tage = STANDARD_TAGE
    return {"auto": (meta_lesen(db, "sicherung.auto") or "an") == "an", "tage": tage,
            "ordner": meta_lesen(db, "sicherung.ordner") or None}


def erstellen(grund: str = "", zusatz_ordner: str | None = None, tage: int = STANDARD_TAGE) -> Path:
    """Sicherung anlegen. Die Datenbank wird mit der SQLite-Sicherungsfunktion kopiert – konsistent, auch
    während der Server läuft."""
    from app.routers.auth import API_VERSION

    s = get_settings()
    ziel_ordner = ablage()
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    jetzt = datetime.now(BERLIN)
    name = f"taleward-sicherung-{jetzt:%Y%m%d-%H%M%S}{'-' + grund if grund else ''}.zip"
    with tempfile.TemporaryDirectory(dir=ziel_ordner) as tmp:
        db_kopie = Path(tmp) / "chronik.db"
        quelle = sqlite3.connect(s.data_dir / "chronik.db")
        ziel = sqlite3.connect(db_kopie)
        try:
            quelle.backup(ziel)
            revision = ziel.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            quelle.close()
            ziel.close()
        teil = Path(tmp) / (name + ".part")
        dateien = 0
        with zipfile.ZipFile(teil, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(db_kopie, "chronik.db")
            for unter in ORDNER_IM_ZIP:
                basis = s.data_dir / unter
                if not basis.exists():
                    continue
                for datei in sorted(basis.rglob("*")):
                    if datei.is_file() and not datei.name.endswith((".tmp", ".part")):
                        z.write(datei, f"{unter}/{datei.relative_to(basis).as_posix()}",
                                compress_type=zipfile.ZIP_STORED if datei.suffix in (".jpg", ".webp", ".pdf",
                                                                                      ".docx") else None)
                        dateien += 1
            z.writestr("manifest.json", json.dumps({
                "format": "taleward-sicherung/1", "erstellt": jetzt.isoformat(), "schnittstelle": API_VERSION,
                "datenbank_revision": revision[0] if revision else None, "dateien": dateien,
            }, indent=2))
        endgueltig = ziel_ordner / name
        teil.replace(endgueltig)
    if zusatz_ordner:
        try:
            shutil.copy2(endgueltig, Path(zusatz_ordner) / name)
        except OSError:
            log.exception("Sicherung konnte nicht nach %s kopiert werden", zusatz_ordner)
    aufraeumen(tage, zusatz_ordner)
    log.info("Sicherung angelegt: %s (%d Dateien)", name, dateien)
    return endgueltig


def aufraeumen(tage: int, zusatz_ordner: str | None = None) -> None:
    """Alte automatische Sicherungen löschen. Die neuesten drei bleiben immer."""
    grenze = datetime.now(timezone.utc) - timedelta(days=max(1, tage))
    for ordner in [ablage()] + ([Path(zusatz_ordner)] if zusatz_ordner else []):
        try:
            dateien = sorted((p for p in ordner.iterdir() if NAME.match(p.name)), key=lambda p: p.stat().st_mtime,
                             reverse=True)
        except OSError:
            continue
        for p in dateien[3:]:
            if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < grenze:
                p.unlink(missing_ok=True)


def faellig(db) -> bool:
    e = einstellungen(db)
    if not e["auto"]:
        return False
    letzte = liste()
    return not letzte or letzte[0].zeit < datetime.now(timezone.utc) - timedelta(hours=24)


def automatisch(db) -> Path | None:
    """Aus der Wartung: jede Nacht (bzw. wenn die letzte älter als 24 Stunden ist) eine Sicherung."""
    if not faellig(db):
        return None
    e = einstellungen(db)
    return erstellen("", e["ordner"], e["tage"])


# ---------------------------------------------------------------- Wiederherstellen
class SicherungFehler(Exception):
    pass


def pruefen(datei: Path) -> dict:
    try:
        with zipfile.ZipFile(datei) as z:
            namen = set(z.namelist())
            if "chronik.db" not in namen or "manifest.json" not in namen:
                raise SicherungFehler("Das ist keine Taleward-Sicherung (chronik.db oder manifest.json fehlt).")
            for n in namen:
                if n.startswith("/") or ".." in Path(n).parts or not (
                        n in ("chronik.db", "manifest.json") or n.split("/")[0] in ORDNER_IM_ZIP):
                    raise SicherungFehler(f"Unerwarteter Inhalt in der Sicherung: {n}")
            return json.loads(z.read("manifest.json"))
    except zipfile.BadZipFile:
        raise SicherungFehler("Die Datei ist keine gültige ZIP-Datei.") from None


def wiederherstellen(datei: Path) -> Path:
    """Nur bei gestopptem Server aufrufen (chronik wiederherstellen). Vorher wird der jetzige Stand gesichert.
    Liefert den Pfad dieser Vorher-Sicherung."""
    pruefen(datei)
    s = get_settings()
    vorher = erstellen("vorher") if (s.data_dir / "chronik.db").exists() else None
    with tempfile.TemporaryDirectory(dir=s.data_dir) as tmp:
        with zipfile.ZipFile(datei) as z:
            z.extractall(tmp)
        for endung in ("", "-wal", "-shm"):
            (s.data_dir / f"chronik.db{endung}").unlink(missing_ok=True)
        os.replace(Path(tmp) / "chronik.db", s.data_dir / "chronik.db")
        for unter in ORDNER_IM_ZIP:
            shutil.rmtree(s.data_dir / unter, ignore_errors=True)
            if (Path(tmp) / unter).exists():
                shutil.move(str(Path(tmp) / unter), str(s.data_dir / unter))
    return vorher
