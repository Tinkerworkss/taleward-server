"""Einstellungen des eingebauten Workers (Docker-Paket), gepflegt in der Verwaltung.

Der Worker-Container holt sie über das Worker-Protokoll (/worker/v1/config, Feld „eingebaut“) – nur der lokale
Worker bekommt sie. Nicht gesetzte Werte lässt der Worker, wie sie in seiner Umgebung stehen (.env/Compose).
Ändert sich etwas, beendet sich der Worker nach dem laufenden Auftrag; Docker startet ihn mit den neuen Werten neu.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben

MODELLE = ("auto", "large-v3", "large-v3-turbo")
MAX_VRAM_MB = 98304   # 96 GB – mehr hat keine Karte, die hier sinnvoll ist
MAX_THREADS = 256


def _zahl(text: str | None, hoechstens: int) -> int | None:
    try:
        wert = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return max(0, min(hoechstens, wert))


def lesen(db: Session) -> dict:
    """Gespeicherte Werte; None = nicht in der Verwaltung gesetzt (dann gilt die Umgebung des Workers)."""
    modell = meta_lesen(db, "eingebaut.modell")
    prozessor = meta_lesen(db, "eingebaut.prozessor")
    return {
        "vramMb": _zahl(meta_lesen(db, "eingebaut.vram_mb"), MAX_VRAM_MB),
        "modell": modell if modell in MODELLE else None,
        "prozessor": None if prozessor is None else prozessor == "1",
        "threads": _zahl(meta_lesen(db, "eingebaut.threads"), MAX_THREADS),
        "neustart": meta_lesen(db, "eingebaut.neustart"),
    }


def fuer_worker(db: Session) -> dict:
    """Nur gesetzte Werte – so kann der Worker unterscheiden, was er selbst bestimmen soll."""
    return {k: v for k, v in lesen(db).items() if v is not None}


def speichern(db: Session, vram_mb: str | None, modell: str | None, prozessor: bool | None,
              threads: str | None) -> None:
    """Werte aus dem Formular übernehmen und einen Neustart des Workers anstoßen."""
    if vram_mb is not None:
        wert = _zahl(vram_mb, MAX_VRAM_MB)
        if wert is not None:
            meta_schreiben(db, "eingebaut.vram_mb", str(wert))
    if modell in MODELLE:
        meta_schreiben(db, "eingebaut.modell", modell)
    if prozessor is not None:
        meta_schreiben(db, "eingebaut.prozessor", "1" if prozessor else "0")
    if threads is not None:
        wert = _zahl(threads, MAX_THREADS)
        if wert is not None:
            meta_schreiben(db, "eingebaut.threads", str(wert))
    neu_starten(db)


def neu_starten(db: Session) -> None:
    meta_schreiben(db, "eingebaut.neustart", utcnow().isoformat())


# ---------------------------------------------------------------- Messwerte (alle Worker)
def messung_merken(db: Session, worker_id: str, audio_sekunden: float, rechen_sekunden: float,
                   modell: str | None, peak_vram_mb: int | None) -> None:
    meta_schreiben(db, f"worker.messung.{worker_id}", json.dumps({
        "zeit": utcnow().isoformat(), "audioSekunden": round(audio_sekunden, 1),
        "rechenSekunden": round(rechen_sekunden, 1), "modell": modell, "peakVramMb": peak_vram_mb,
    }))


def messung(db: Session, worker_id: str) -> dict | None:
    roh = meta_lesen(db, f"worker.messung.{worker_id}")
    try:
        return json.loads(roh) if roh else None
    except ValueError:
        return None
