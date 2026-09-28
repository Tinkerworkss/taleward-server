"""Wo die App ihre Dateien ablegt – alles in einem Ordner je Benutzer, nichts im Programmordner."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir

WINDOWS = sys.platform == "win32"


def basis() -> Path:
    """Windows: %LOCALAPPDATA%\\Taleward Worker, Linux: ~/.local/share/taleward-worker.
    TALEWARD_WORKER_HOME überschreibt das (Tests, mehrere Worker auf einem PC)."""
    eigen = os.environ.get("TALEWARD_WORKER_HOME")
    if eigen:
        p = Path(eigen)
    elif WINDOWS:
        p = Path(user_data_dir("Taleward Worker", appauthor=False, roaming=False))
    else:
        p = Path(user_data_dir("taleward-worker", appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def motor() -> Path:
    """Python-Umgebung mit dem KI-Paket."""
    return basis() / "motor"


def motor_python() -> Path:
    return motor() / ("Scripts/python.exe" if WINDOWS else "bin/python")


def motor_chronik() -> Path:
    return motor() / ("Scripts/chronik.exe" if WINDOWS else "bin/chronik")


def werkzeuge() -> Path:
    """Eigene Werkzeuge der App (ffmpeg als Verknüpfung/Kopie), vorn im PATH des Motors."""
    p = basis() / "werkzeuge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def modelle() -> Path:
    return basis() / "modelle"


def arbeit() -> Path:
    return basis() / "arbeit"


def protokolle() -> Path:
    p = basis() / "protokolle"
    p.mkdir(parents=True, exist_ok=True)
    return p


def einstellungen() -> Path:
    return basis() / "einstellungen.json"


def ressourcen() -> Path:
    """Mitgelieferte Dateien (Oberfläche, Symbole, uv). Im PyInstaller-Paket unter sys._MEIPASS."""
    wurzel = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return wurzel / "taleward_worker" if (wurzel / "taleward_worker").is_dir() else Path(__file__).resolve().parent


def ordnergroesse(p: Path) -> int:
    gesamt = 0
    for wurzel, _, dateien in os.walk(p):
        for d in dateien:
            try:
                gesamt += os.lstat(os.path.join(wurzel, d)).st_size
            except OSError:
                pass
    return gesamt
