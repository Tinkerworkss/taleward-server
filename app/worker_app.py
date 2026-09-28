"""Anbindung an die Worker-App (Oberfläche für Windows und Linux, Repository taleward-worker).

Die App startet „chronik worker --app“ als Unterprozess:
- Ereignisse gehen als eine JSON-Zeile je Ereignis auf stdout, mit Vorsilbe, damit andere Ausgaben nicht stören:
  ``@@taleward {"ereignis": "fortschritt", "jobId": "…", "p": 0.42}``
- Steuerung kommt zeilenweise über stdin: ``pause``, ``weiter``, ``stopp``. Schließt die App stdin (oder stürzt sie
  ab), beendet sich der Worker ebenfalls.

Ereignisse: pruefe, bereit, warte, auftrag, fortschritt, fertig, fehlgeschlagen, abgebrochen, getrennt, verbunden,
pausiert, fortgesetzt, abgelehnt, fehler, beendet. Inhalte von Aufnahmen oder Texten stehen nie darin.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

VORSILBE = "@@taleward "


class Anbindung:
    def __init__(self, aus=None, ein=None):
        self.aus = aus or sys.stdout
        self.ein = ein or sys.stdin
        self._sperre = threading.Lock()

    def melden(self, ereignis: str, **daten) -> None:
        zeile = VORSILBE + json.dumps({"ereignis": ereignis, "zeit": round(time.time(), 1), **daten},
                                      ensure_ascii=False)
        with self._sperre:
            try:
                self.aus.write(zeile + "\n")
                self.aus.flush()
            except (OSError, ValueError):
                pass  # App schon weg

    def steuern(self, worker, beenden=None) -> threading.Thread:
        """Liest Befehle der App in einem eigenen Faden."""
        beenden = beenden or (lambda: os._exit(0))

        def lesen():
            for zeile in self.ein:
                befehl = zeile.strip().lower()
                if befehl == "pause":
                    worker.pausieren(True)
                elif befehl == "weiter":
                    worker.pausieren(False)
                elif befehl == "stopp":
                    break
            # stopp oder App geschlossen: laufenden Auftrag abbrechen (Server gibt ihn weiter), dann Ende
            worker.beenden()
            time.sleep(2)  # Zeit für die Fehlermeldung „worker_stopped“ an den Server
            worker.aufraeumen()
            self.melden("beendet")
            beenden()

        faden = threading.Thread(target=lesen, daemon=True, name="app-steuerung")
        faden.start()
        return faden
