"""Startet und überwacht den Motor („chronik worker --app“) und hält den Zustand für die Oberfläche.

Der Motor meldet Ereignisse als JSON-Zeilen (siehe app/worker_app.py im Server-Code). Stürzt er ab, startet der
Dienst ihn mit wachsender Wartezeit neu. Während eines Auftrags verhindert er den Ruhezustand des PCs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date

from taleward_worker import hardware, pfade
from taleward_worker.einstellungen import Einstellungen

VORSILBE = "@@taleward "
WARTEZEITEN = [5, 15, 60, 300]      # Neustart nach Absturz
ERNEUT_NACH_EINRICHTUNGSFEHLER = 600


class Wachhalten:
    """Ruhezustand verhindern, solange ein Auftrag läuft (Bildschirm darf aus)."""

    def __init__(self):
        self._aktiv = False
        self._linux: subprocess.Popen | None = None

    def an(self) -> None:
        if self._aktiv:
            return
        self._aktiv = True
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
        else:
            try:
                self._linux = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=Taleward Worker",
                                                "--why=Aufnahme wird verarbeitet", "sleep", "infinity"],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._linux = None

    def aus(self) -> None:
        if not self._aktiv:
            return
        self._aktiv = False
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        elif self._linux:
            self._linux.terminate()
            self._linux = None


class Statistik:
    def __init__(self):
        self.datei = pfade.basis() / "statistik.json"
        try:
            self.d = json.loads(self.datei.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.d = {}

    def erledigt(self, sekunden: float, audio_sekunden: float | None = None, peak_mb: int | None = None) -> None:
        heute = date.today().isoformat()
        if self.d.get("tag") != heute:
            self.d.update({"tag": heute, "heute": 0})
        self.d["heute"] = self.d.get("heute", 0) + 1
        self.d["gesamt"] = self.d.get("gesamt", 0) + 1
        self.d["zuletzt"] = time.time()
        self.d["sekundenGesamt"] = round(self.d.get("sekundenGesamt", 0) + sekunden)
        if audio_sekunden:
            # Zuletzt gemessen: ersetzt die grobe Schätzung in der Oberfläche
            self.d["messung"] = {"audio": round(audio_sekunden), "rechen": round(sekunden), "peakMb": peak_mb,
                                 "zeit": time.time()}
        try:
            self.datei.write_text(json.dumps(self.d), encoding="utf-8")
        except OSError:
            pass

    def fuer_oberflaeche(self) -> dict:
        heute = self.d.get("heute", 0) if self.d.get("tag") == date.today().isoformat() else 0
        return {"heute": heute, "gesamt": self.d.get("gesamt", 0), "zuletzt": self.d.get("zuletzt"),
                "messung": self.d.get("messung")}


class Protokoll:
    def __init__(self, max_bytes: int = 2_000_000):
        self.zeilen: deque[str] = deque(maxlen=1500)
        self.datei = pfade.protokolle() / "worker.log"
        self.max_bytes = max_bytes
        self._sperre = threading.Lock()

    def schreiben(self, text: str) -> None:
        zeile = f"{time.strftime('%d.%m. %H:%M:%S')}  {text}"
        with self._sperre:
            self.zeilen.append(zeile)
            try:
                if self.datei.exists() and self.datei.stat().st_size > self.max_bytes:
                    os.replace(self.datei, self.datei.with_suffix(".log.1"))
                with self.datei.open("a", encoding="utf-8") as f:
                    f.write(zeile + "\n")
            except OSError:
                pass


class Dienst:
    def __init__(self, einstellungen: Einstellungen, befehl: list[str] | None = None):
        self.e = einstellungen
        self._befehl = befehl  # für Tests: eigener Motor-Befehl
        self.protokoll = Protokoll()
        self.statistik = Statistik()
        self.wach = Wachhalten()
        self._prozess: subprocess.Popen | None = None
        self._sperre = threading.RLock()
        self._soll_laufen = False
        self._versuche = 0
        self._neustart_um: float | None = None
        self._neustart_offen = False  # Einstellung geändert: nach dem laufenden Auftrag neu starten
        self.zustand: dict = {"art": "gestoppt"}
        self.info: dict = {}
        self._waechter = threading.Thread(target=self._wachen, daemon=True, name="dienst-waechter")
        self._waechter.start()

    # ------------------------------------------------------------------ Steuerung
    def starten(self) -> None:
        with self._sperre:
            self._soll_laufen = True
            self._versuche = 0
            self._neustart_um = None
            if self._prozess is None or self._prozess.poll() is not None:
                self._motor_starten()

    def stoppen(self, warten: float = 12) -> None:
        with self._sperre:
            self._soll_laufen = False
            self._neustart_um = None
            p = self._prozess
        if p and p.poll() is None:
            self._senden("stopp")
            try:
                p.wait(warten)
            except subprocess.TimeoutExpired:
                p.kill()
        self.wach.aus()
        self._setzen("gestoppt")

    def neu_starten(self) -> None:
        self.stoppen()
        self.starten()

    def neu_starten_wenn_frei(self) -> None:
        """Neue Einstellungen übernehmen, ohne einen laufenden Auftrag abzubrechen."""
        if not self.laeuft():
            return
        if self.zustand.get("art") == "arbeitet":
            self._neustart_offen = True
        else:
            threading.Thread(target=self.neu_starten, daemon=True).start()

    def profil(self) -> dict:
        karten = hardware.grafikkarten()
        return hardware.profil(karten[0].vram_mb if karten else None, self.e.vram_grenze_mb, self.e.modell,
                               prozessor=self.e.geraet == "cpu")

    def pausieren(self, an: bool) -> None:
        self.e.pausiert = an
        self.e.speichern()
        self._senden("pause" if an else "weiter")
        if an and self.zustand["art"] in ("warte", "getrennt"):
            self._setzen("pausiert")

    def laeuft(self) -> bool:
        return self._prozess is not None and self._prozess.poll() is None

    # ------------------------------------------------------------------ Motor
    def _motor_befehl(self) -> list[str]:
        if self._befehl:
            return list(self._befehl)
        eigen = os.environ.get("TALEWARD_MOTOR")  # Entwicklung: vorhandene Umgebung nutzen
        py = eigen or str(pfade.motor_python())
        befehl = [py, "-m", "app.cli", "worker", "--app"]
        if self.e.testmodus:
            befehl.append("--testmodus")
        if self.e.unsicher:
            befehl.append("--unsicher")
        return befehl

    def _motor_umgebung(self) -> dict:
        env = dict(os.environ)
        wahl = self.profil()
        env.update({
            "WHISPER_DEVICE": wahl["geraet"], "ALIGN_DEVICE": wahl["ausrichten"], "DIARIZE_DEVICE": wahl["sprecher"],
            "WHISPER_COMPUTE_TYPE": wahl["genauigkeit"], "GPU_MEMORY_LIMIT_MB": str(wahl["grenzeMb"]),
            "CPU_THREADS": str(max(1, (os.cpu_count() or 2) - 1)) if wahl["geraet"] == "cpu" else "0",
            "WORKER_SERVER_URL": self.e.server, "WORKER_TOKEN": self.e.token,
            "WORKER_WORK_DIR": str(pfade.arbeit()), "HF_HOME": str(pfade.modelle()),
            "DATA_DIR": str(pfade.basis() / "motor-daten"),
            "WHISPER_MODEL": wahl["modell"], "WHISPER_BATCH": str(wahl["batch"]),
            "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
            "PATH": str(pfade.werkzeuge()) + os.pathsep + env.get("PATH", ""),
        })
        for schluessel in ("VIRTUAL_ENV", "HF_TOKEN", "DATABASE_URL"):
            env.pop(schluessel, None)
        return env

    def _motor_starten(self) -> None:
        befehl = self._motor_befehl()
        if not self._befehl and not os.environ.get("TALEWARD_MOTOR") and not pfade.motor_python().exists():
            self._setzen("fehler", code="motor_fehlt")
            self._soll_laufen = False
            return
        self.protokoll.schreiben("Worker startet …")
        self._setzen("startet")
        extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
        try:
            self._prozess = subprocess.Popen(befehl, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                             bufsize=1, env=self._motor_umgebung(), cwd=pfade.basis(), **extra)
        except OSError as e:
            self._setzen("fehler", code="start_fehlgeschlagen", text=str(e))
            self._soll_laufen = False
            return
        threading.Thread(target=self._lesen, args=(self._prozess,), daemon=True, name="motor-ausgabe").start()
        if self.e.pausiert:
            self._senden("pause")

    def _senden(self, befehl: str) -> None:
        p = self._prozess
        if p and p.poll() is None and p.stdin:
            try:
                p.stdin.write(befehl + "\n")
                p.stdin.flush()
            except (OSError, ValueError):
                pass

    def _lesen(self, p: subprocess.Popen) -> None:
        letzter_fehler = ""
        for zeile in p.stdout:
            zeile = zeile.rstrip()
            if zeile.startswith(VORSILBE):
                try:
                    self._ereignis(json.loads(zeile[len(VORSILBE):]))
                except ValueError:
                    pass
            elif zeile:
                zeile = re.sub(r"^\d\d:\d\d:\d\d ", "", zeile)  # Uhrzeit steht schon vorn im Protokoll
                self.protokoll.schreiben(zeile)
                if "Worker kann nicht starten" in zeile or "Error" in zeile:
                    letzter_fehler = zeile
        code = p.wait()
        self.wach.aus()
        with self._sperre:
            if p is not self._prozess:
                return
            art = self.zustand["art"]
            if not self._soll_laufen:
                self._setzen("gestoppt")
            elif art == "abgelehnt":
                self._soll_laufen = False
            elif art == "fehler":  # Einrichtungsfehler (z. B. Modell fehlt auf dem Server): später erneut
                self._neustart_um = time.monotonic() + ERNEUT_NACH_EINRICHTUNGSFEHLER
                self.zustand["neuerVersuch"] = time.time() + ERNEUT_NACH_EINRICHTUNGSFEHLER
            else:
                warte = WARTEZEITEN[min(self._versuche, len(WARTEZEITEN) - 1)]
                self._versuche += 1
                self.protokoll.schreiben(f"Worker unerwartet beendet (Code {code}) – Neustart in {warte} s")
                self._setzen("abgestuerzt", code=str(code), text=letzter_fehler, neuerVersuch=time.time() + warte)
                self._neustart_um = time.monotonic() + warte

    def _wachen(self) -> None:
        while True:
            time.sleep(1)
            with self._sperre:
                if self._soll_laufen and self._neustart_um and time.monotonic() >= self._neustart_um:
                    self._neustart_um = None
                    self._motor_starten()
            if self.zustand["art"] == "startet" and self.laeuft():
                groesse = pfade.ordnergroesse(pfade.modelle()) if pfade.modelle().exists() else 0
                self.zustand["modelleMb"] = groesse // 2 ** 20

    # ------------------------------------------------------------------ Ereignisse des Motors
    def _setzen(self, art: str, **daten) -> None:
        self.zustand = {"art": art, "seit": time.time(), **daten}

    def _ereignis(self, e: dict) -> None:
        art = e.get("ereignis")
        if art == "pruefe":
            self._setzen("startet", modelle=True)
        elif art == "bereit":
            self.info = {k: e.get(k) for k in ("gpu", "modell", "vramMb", "llm", "testmodus", "geraete", "grenzeMb")}
            self._versuche = 0
            teile = [e.get("gpu") or "Testmodus", e.get("modell"), e.get("llm")]
            self.protokoll.schreiben("Bereit: " + " · ".join(str(x) for x in teile if x))
        elif art == "warte" or art == "verbunden":
            self.wach.aus()
            if self._neustart_offen:
                self._neustart_offen = False
                threading.Thread(target=self.neu_starten, daemon=True).start()
            self._setzen("pausiert" if self.e.pausiert else "warte")
        elif art == "auftrag":
            self.wach.an()
            self._setzen("arbeitet", jobId=e.get("jobId"), typ=e.get("typ"), p=0.0)
            self.protokoll.schreiben(f"Auftrag übernommen: {e.get('typ')} ({e.get('dateien')} Datei(en))")
        elif art == "fortschritt":
            if self.zustand["art"] == "arbeitet":
                self.zustand["p"] = e.get("p", 0)
        elif art == "fertig":
            self.statistik.erledigt(e.get("sekunden") or 0, e.get("audioSekunden"), e.get("peakVramMb"))
            self.protokoll.schreiben(f"Auftrag fertig nach {round((e.get('sekunden') or 0) / 60, 1)} min")
        elif art == "fehlgeschlagen":
            self.protokoll.schreiben(f"Auftrag fehlgeschlagen: {e.get('code')} – {e.get('message')}")
            self.letzter_fehlschlag = {"code": e.get("code"), "zeit": time.time()}
        elif art == "abgebrochen":
            self.wach.aus()
        elif art == "getrennt":
            self.wach.aus()
            self._setzen("getrennt", grund=e.get("grund"), neuerVersuch=time.time() + (e.get("wiederIn") or 0))
        elif art == "pausiert":
            if self.zustand["art"] != "arbeitet":
                self._setzen("pausiert")
        elif art == "fortgesetzt":
            if self.zustand["art"] == "pausiert":
                self._setzen("warte")
        elif art == "abgelehnt":
            self._setzen("abgelehnt")
        elif art == "fehler":
            self._setzen("fehler", code="einrichtung", text=e.get("message", ""))
            self.protokoll.schreiben(f"Fehler: {e.get('message')}")

    def fuer_oberflaeche(self) -> dict:
        return {"zustand": self.zustand, "info": self.info, "statistik": self.statistik.fuer_oberflaeche(),
                "laeuft": self.laeuft(), "pausiert": self.e.pausiert, "neustartOffen": self._neustart_offen}
