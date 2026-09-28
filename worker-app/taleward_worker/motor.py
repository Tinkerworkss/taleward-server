"""KI-Paket („Motor“) installieren und aktualisieren.

Der Motor ist der Taleward-Server-Code in genau der Fassung des verbundenen Servers, mit den KI-Bibliotheken
(WhisperX, PyTorch mit CUDA). Die App installiert ihn mit uv in eine eigene Python-Umgebung im Benutzerordner:

1. Python 3.11 (von uv verwaltet, unabhängig von einem installierten Python)
2. Pakete aus engine-requirements.txt der passenden Fassung (feste Versionen wie auf dem Server getestet);
   PyTorch in der Variante für die vorhandene Grafikkarte (uv --torch-backend auto)
3. der Taleward-Code selbst (ohne Abhängigkeiten, die kamen aus Schritt 2) und ffmpeg (imageio-ffmpeg)

Im Testmodus reicht der Taleward-Code mit den Grundpaketen (ein paar MB statt einiger GB).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from taleward_worker import REPO, VERSION, pfade

# Ungefähre Downloadgröße (für die Fortschrittsanzeige; gemessen an der Größe des Zwischenspeichers)
ERWARTET_MB = {"windows": 3600, "linux": 5200, "cpu": 1300, "test": 60}


class MotorFehler(Exception):
    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code
        self.text = text


def _ohne_fenster() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def uv_programm() -> str:
    """uv liegt dem Windows-Paket bei (ressourcen/uv.exe); unter Linux installiert es das Installationsskript."""
    name = "uv.exe" if sys.platform == "win32" else "uv"
    mitgeliefert = pfade.ressourcen() / "bin" / name
    if mitgeliefert.exists():
        return str(mitgeliefert)
    gefunden = shutil.which("uv") or shutil.which(str(Path.home() / ".local" / "bin" / "uv"))
    if gefunden:
        return gefunden
    raise MotorFehler("uv_fehlt")


def quelle(fassung: str) -> dict:
    """Download-Adressen für eine Serverfassung. Gibt es kein passendes Git-Tag (v0.4.0), gilt der main-Zweig."""
    eigen = os.environ.get("TALEWARD_MOTOR_QUELLE")  # Entwicklung: lokaler Ordner des Server-Codes
    if eigen:
        return {"ref": "lokal", "genau": True, "paket": eigen,
                "anforderungen": str(Path(eigen) / "engine-requirements.txt")}
    kandidaten = [f"v{fassung}", fassung] if fassung and fassung != "0.0.0" else []
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": f"TalewardWorker/{VERSION}"}) as k:
        for ref in kandidaten:
            try:
                r = k.head(f"https://raw.githubusercontent.com/{REPO}/{ref}/engine-requirements.txt")
            except httpx.HTTPError:
                raise MotorFehler("github_nicht_erreichbar") from None
            if r.status_code == 200:
                return _adressen(ref, genau=True)
    return _adressen("main", genau=False)


def _adressen(ref: str, genau: bool) -> dict:
    art = "tags" if ref != "main" else "heads"
    return {"ref": ref, "genau": genau,
            "paket": f"https://github.com/{REPO}/archive/refs/{art}/{ref}.zip",
            "anforderungen": f"https://raw.githubusercontent.com/{REPO}/{ref}/engine-requirements.txt"}


@dataclass
class Installation:
    """Läuft in einem eigenen Faden; die Oberfläche fragt den Stand ab."""
    fassung: str
    testmodus: bool = False
    backend: str = "cuda"         # "cuda" (NVIDIA) oder "cpu" (nur Prozessor, deutlich kleiner)
    phase: str = "start"          # start, python, pakete, taleward, ffmpeg, fertig, fehler, abgebrochen
    anteil: float = 0.0
    geladen_mb: int = 0
    erwartet_mb: int = 0
    fehler: str = ""
    fehlertext: str = ""
    zeilen: list[str] = field(default_factory=list)
    vor_tausch: object = None     # Aufruf vor dem Ersetzen des alten Motors (laufenden Worker anhalten)
    nach_tausch: object = None
    _prozess: subprocess.Popen | None = None
    _abbrechen: threading.Event = field(default_factory=threading.Event)

    def stand(self) -> dict:
        return {"phase": self.phase, "anteil": round(self.anteil, 3), "geladenMb": self.geladen_mb,
                "erwartetMb": self.erwartet_mb, "fehler": self.fehler, "fehlertext": self.fehlertext,
                "fassung": self.fassung, "letzteZeile": self.zeilen[-1] if self.zeilen else ""}

    def abbrechen(self) -> None:
        self._abbrechen.set()
        if self._prozess and self._prozess.poll() is None:
            self._prozess.kill()

    # ------------------------------------------------------------------ Ablauf
    def starten(self) -> threading.Thread:
        t = threading.Thread(target=self._lauf, daemon=True, name="motor-installation")
        t.start()
        return t

    def _lauf(self) -> None:
        try:
            self._installieren()
            self.phase, self.anteil = "fertig", 1.0
        except MotorFehler as e:
            self.phase = "abgebrochen" if self._abbrechen.is_set() else "fehler"
            self.fehler, self.fehlertext = e.code, e.text
        except Exception as e:  # unerwartet – Text für das Protokoll
            self.phase, self.fehler, self.fehlertext = "fehler", "unerwartet", f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(pfade.basis() / "cache", ignore_errors=True)  # spart einige GB

    def _umgebung(self) -> dict:
        env = dict(os.environ)
        env.update({"UV_CACHE_DIR": str(pfade.basis() / "cache"),
                    "UV_PYTHON_INSTALL_DIR": str(pfade.basis() / "python"),
                    "UV_PYTHON_PREFERENCE": "only-managed",
                    "UV_NO_PROGRESS": "1", "NO_COLOR": "1", "UV_LINK_MODE": "copy"})
        env.pop("VIRTUAL_ENV", None)
        return env

    def _uv(self, *argumente: str, anteil_von: float, anteil_bis: float, erwartet_mb: int = 0) -> None:
        if self._abbrechen.is_set():
            raise MotorFehler("abgebrochen")
        befehl = [uv_programm(), *argumente]
        self.zeilen.append("$ uv " + " ".join(argumente))
        cache = pfade.basis() / "cache"
        start_mb = pfade.ordnergroesse(cache) // 2 ** 20 if cache.exists() else 0
        self._prozess = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", env=self._umgebung(),
                                         cwd=pfade.basis(), **_ohne_fenster())
        lesen = threading.Thread(target=self._mitlesen, args=(self._prozess,), daemon=True)
        lesen.start()
        while self._prozess.poll() is None:
            if erwartet_mb:
                self.geladen_mb = max(0, pfade.ordnergroesse(cache) // 2 ** 20 - start_mb) if cache.exists() else 0
                self.anteil = anteil_von + (anteil_bis - anteil_von) * min(0.97, self.geladen_mb / erwartet_mb)
            time.sleep(1)
        lesen.join(2)
        if self._prozess.returncode != 0:
            if self._abbrechen.is_set():
                raise MotorFehler("abgebrochen")
            letzte = "\n".join(self.zeilen[-12:])
            code = "kein_platz" if "No space left" in letzte or "Speicherplatz" in letzte else "installation"
            code = "netz" if code == "installation" and ("dns error" in letzte or "timed out" in letzte
                                                        or "Failed to fetch" in letzte) else code
            raise MotorFehler(code, letzte)
        self.anteil = anteil_bis

    def _mitlesen(self, p: subprocess.Popen) -> None:
        for zeile in p.stdout:
            zeile = zeile.rstrip()
            if zeile:
                self.zeilen.append(zeile)
                del self.zeilen[:-400]

    def _installieren(self) -> None:
        q = quelle(self.fassung)
        self.zeilen.append(f"Fassung {self.fassung} → {q['ref']}" + ("" if q["genau"] else " (kein Tag, main)"))
        system = "test" if self.testmodus else "cpu" if self.backend == "cpu" else (
            "windows" if sys.platform == "win32" else "linux")
        self.erwartet_mb = ERWARTET_MB[system]
        neu = pfade.basis() / "motor-neu"
        shutil.rmtree(neu, ignore_errors=True)

        self.phase = "python"
        self._uv("python", "install", "3.11", anteil_von=0.0, anteil_bis=0.05, erwartet_mb=30)
        self._uv("venv", str(neu), "--python", "3.11", anteil_von=0.05, anteil_bis=0.06)
        py = str(neu / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))

        self.phase = "pakete"
        if self.testmodus:
            self._uv("pip", "install", "--python", py, q["paket"], "imageio-ffmpeg",
                     anteil_von=0.06, anteil_bis=0.95, erwartet_mb=self.erwartet_mb)
        else:
            torch = "cpu" if self.backend == "cpu" else "auto"  # auto: CUDA-Fassung passend zum Treiber
            self._uv("pip", "install", "--python", py, "--torch-backend", torch, "-r", q["anforderungen"],
                     "imageio-ffmpeg", anteil_von=0.06, anteil_bis=0.9, erwartet_mb=self.erwartet_mb)
            self.phase = "taleward"
            self._uv("pip", "install", "--python", py, "--no-deps", q["paket"], anteil_von=0.9, anteil_bis=0.95)

        self.phase = "ffmpeg"
        pruefung = subprocess.run([py, "-c", "import app, sys; print('ok')"], capture_output=True, text=True,
                                  timeout=120, **_ohne_fenster())
        if "ok" not in pruefung.stdout:
            raise MotorFehler("installation", pruefung.stderr[-2000:])

        # Erst jetzt den alten Motor ersetzen – bei einem Fehler bleibt der bisherige nutzbar
        alt = pfade.motor()
        weg = pfade.basis() / "motor-alt"
        shutil.rmtree(weg, ignore_errors=True)
        if callable(self.vor_tausch):
            self.vor_tausch()
        try:
            if alt.exists():
                alt.rename(weg)
            neu.rename(alt)
            ffmpeg_einrichten(pfade.motor_python())
        finally:
            if callable(self.nach_tausch):
                self.nach_tausch()
        shutil.rmtree(weg, ignore_errors=True)
        # Hinweis: Skripte im Motor (chronik.exe) kennen noch den alten Ordnernamen – gestartet wird daher immer
        # mit „python -m app.cli“, das funktioniert nach dem Umbenennen weiter.
        (alt / "taleward-motor.json").write_text(json.dumps(
            {"fassung": self.fassung, "ref": q["ref"], "testmodus": self.testmodus, "backend": self.backend,
             "installiert": time.strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")


def ffmpeg_einrichten(python: Path) -> Path:
    """ffmpeg aus imageio-ffmpeg als „ffmpeg(.exe)“ in den Werkzeug-Ordner legen (WhisperX ruft „ffmpeg“ auf)."""
    aus = subprocess.run([str(python), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                         capture_output=True, text=True, timeout=60, **_ohne_fenster())
    quelle_pfad = Path(aus.stdout.strip())
    if aus.returncode != 0 or not quelle_pfad.exists():
        raise MotorFehler("ffmpeg", aus.stderr[-1000:])
    ziel = pfade.werkzeuge() / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    ziel.unlink(missing_ok=True)
    shutil.copy2(quelle_pfad, ziel)
    if sys.platform != "win32":
        ziel.chmod(0o755)
    return ziel


def installiert() -> dict | None:
    try:
        d = json.loads((pfade.motor() / "taleward-motor.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if pfade.motor_python().exists() else None


def entfernen(modelle_auch: bool = False) -> None:
    for p in [pfade.motor(), pfade.basis() / "motor-neu", pfade.basis() / "python", pfade.basis() / "cache",
              pfade.werkzeuge()]:
        shutil.rmtree(p, ignore_errors=True)
    if modelle_auch:
        shutil.rmtree(pfade.modelle(), ignore_errors=True)
