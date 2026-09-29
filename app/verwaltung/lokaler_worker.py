"""Lokaler Worker: von der Verwaltung gestartet, läuft als eigener Prozess auf demselben Server.

- Er spricht wie jeder Worker über das Worker-Protokoll mit der Zentrale (localhost), nicht direkt mit der
  Datenbank – nur ein Weg im Code.
- Bei jedem Start bekommt er einen frischen Zugangsschlüssel. Das Geheimnis steht nirgends gespeichert, nur seine
  Prüfsumme (wie bei allen Workern).
- Beenden per SIGINT: Der Worker meldet einen laufenden Auftrag zurück und räumt seinen Arbeitsordner auf.
- Soll er beim Serverstart mitstarten, steht das in server_meta („lokaler_worker“ = echt | attrappe | aus).
"""
from __future__ import annotations

import importlib.util
import os
import secrets
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import Worker

NAME = "Lokaler Worker"
META_KEY = "lokaler_worker"
LOG_MAX = 2 * 1024 * 1024


def ki_verfuegbar() -> bool:
    """Sind die KI-Pakete installiert? (Ob die Grafikkarte geht, prüft der Worker selbst beim Start.)"""
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "whisperx"))


def standard_befehl(modus: str, server_url: str) -> list[str]:
    befehl = [sys.executable, "-c", "from app.cli import app; app()", "worker", "--server", server_url]
    if modus == "attrappe":
        befehl.append("--testmodus")
    return befehl


class LokalerKnecht:
    def __init__(self, befehl: Callable[[str, str], list[str]] = standard_befehl):
        self._befehl = befehl
        self._proc: subprocess.Popen | None = None
        self._modus: str | None = None
        self._gestartet: datetime | None = None
        self._lock = threading.Lock()

    # -- Zustand
    @property
    def logdatei(self) -> Path:
        return get_settings().data_dir / "logs" / "worker.log"

    def laeuft(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def zustand(self) -> dict:
        code = self._proc.poll() if self._proc is not None else None
        return {"laeuft": self.laeuft(), "modus": self._modus, "gestartet": self._gestartet,
                "exitcode": code if self._proc is not None and code is not None else None}

    def log_ende(self, zeilen: int = 60) -> list[str]:
        try:
            with open(self.logdatei, encoding="utf-8", errors="replace") as f:
                return [z.rstrip("\n") for z in deque(f, maxlen=zeilen)]
        except FileNotFoundError:
            return []

    # -- Steuern
    def _worker_zeile(self, db: Session, geheim: str) -> Worker:
        from app.routers.worker import token_hash

        w = db.scalar(select(Worker).where(Worker.local.is_(True)))
        if w is None:
            w = Worker(name=NAME, token_hash="", capabilities="asr,llm", local=True)
            db.add(w)
            db.flush()
        w.token_hash, w.revoked_at, w.paused = token_hash(geheim), None, False
        return w

    def starten(self, db: Session, modus: str) -> None:
        if modus not in ("echt", "attrappe"):
            raise ValueError(modus)
        with self._lock:
            if self.laeuft():
                self._beenden_intern()
            geheim = secrets.token_urlsafe(32)
            w = self._worker_zeile(db, geheim)
            db.commit()
            s = get_settings()
            self.logdatei.parent.mkdir(parents=True, exist_ok=True)
            if self.logdatei.exists() and self.logdatei.stat().st_size > LOG_MAX:
                self.logdatei.unlink()
            log = open(self.logdatei, "a", encoding="utf-8")
            log.write(f"\n===== {utcnow():%Y-%m-%d %H:%M} UTC – Start ({modus}) =====\n")
            log.flush()
            env = {**os.environ, "WORKER_TOKEN": f"wk.{w.id}.{geheim}", "PYTHONUNBUFFERED": "1",
                   "WORKER_WORK_DIR": str(s.data_dir / "worker")}
            self._proc = subprocess.Popen(self._befehl(modus, s.worker_server_url), stdout=log,
                                          stderr=subprocess.STDOUT, env=env, cwd=os.getcwd(),
                                          start_new_session=True)
            log.close()  # der Kindprozess hat seine eigene Kopie
            self._modus, self._gestartet = modus, utcnow()

    def _beenden_intern(self, warten: float = 20.0) -> None:
        p = self._proc
        if p is None or p.poll() is not None:
            return
        try:
            p.send_signal(signal.SIGINT)  # wie Strg+C: Auftrag zurückmelden, aufräumen
            p.wait(timeout=warten)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)

    def beenden(self, warten: float = 20.0) -> None:
        with self._lock:
            self._beenden_intern(warten)

    def autostart(self, db: Session) -> None:
        from app.einstellungen import meta_lesen

        if get_settings().worker_art:
            return  # der eingebaute Worker läuft als eigener Container und nutzt denselben Eintrag

        modus = meta_lesen(db, META_KEY, "aus")
        if modus in ("echt", "attrappe") and not self.laeuft():
            self.starten(db, modus)


KNECHT = LokalerKnecht()


# ---------------------------------------------------------------- eingebauter Worker (Docker-Paket)
def eingebauten_worker_koppeln(db: Session) -> Path | None:
    """Docker-Paket mit eingebautem Worker: bei jedem Serverstart einen frischen Schlüssel anlegen und in die Datei
    schreiben, die nur Server und Worker-Container sehen (Volume „kopplung“). Kein Kopplungscode nötig.

    Der Worker liest die Datei beim Start; lehnt der Server einen alten Schlüssel ab, beendet er sich, Docker startet
    ihn neu, und er liest den neuen. Gespeichert wird wie bei allen Workern nur die Prüfsumme.
    """
    s = get_settings()
    if not s.worker_art:
        return None
    geheim = secrets.token_urlsafe(32)
    w = KNECHT._worker_zeile(db, geheim)
    w.capabilities = "asr,llm"
    db.commit()
    datei = s.eingebauter_worker_datei
    datei.parent.mkdir(parents=True, exist_ok=True)
    neu = datei.with_suffix(".neu")
    alt_umask = os.umask(0o077)
    try:
        neu.write_text(f"wk.{w.id}.{geheim}\n", encoding="utf-8")
    finally:
        os.umask(alt_umask)
    os.replace(neu, datei)  # nie halb geschrieben lesen
    return datei
