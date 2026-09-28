"""Grafikkarte, Treiber und freier Speicher – ohne das KI-Paket (über nvidia-smi)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from taleward_worker import pfade

MIN_TREIBER = 525      # CUDA 12 (ältere Treiber: PyTorch sieht die Karte nicht)
PLATZ_KI_GB = 14       # KI-Paket (~6 GB) + Modelle (~5 GB) + Luft für Aufnahmen


@dataclass
class Grafikkarte:
    name: str
    vram_mb: int
    treiber: str

    @property
    def treiber_ok(self) -> bool:
        try:
            return int(self.treiber.split(".")[0]) >= MIN_TREIBER
        except ValueError:
            return True


def _ohne_fenster() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW


_ZWISCHENSPEICHER: dict = {}


def grafikkarten(frisch: bool = False) -> list[Grafikkarte]:
    """NVIDIA-Karten über nvidia-smi (60 s zwischengespeichert – die Oberfläche fragt oft)."""
    import time

    alt = _ZWISCHENSPEICHER.get("karten")
    if alt and not frisch and time.monotonic() - alt[0] < 60:
        return alt[1]
    karten = _grafikkarten_lesen()
    _ZWISCHENSPEICHER["karten"] = (time.monotonic(), karten)
    return karten


def _grafikkarten_lesen() -> list[Grafikkarte]:
    programm = shutil.which("nvidia-smi")
    if programm is None and sys.platform == "win32":
        kandidat = r"C:\Windows\System32\nvidia-smi.exe"
        programm = kandidat if os.path.exists(kandidat) else None
    if programm is None:
        return []
    try:
        aus = subprocess.run([programm, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15, **_ohne_fenster()).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    karten = []
    for zeile in aus.strip().splitlines():
        teile = [t.strip() for t in zeile.split(",")]
        if len(teile) >= 3:
            try:
                karten.append(Grafikkarte(teile[0], int(float(teile[1])), teile[2]))
            except ValueError:
                continue
    return karten


# Stufen nach verfügbarem Grafikspeicher (MB, für Taleward insgesamt). Gemessen: large-v3 mit Stapel 8 braucht auf
# der RTX 3060 Ti höchstens ~5,5 GB (plus Windows). Die übrigen Werte sind vorsichtige Schätzungen; die App zeigt
# nach dem ersten Auftrag den gemessenen Höchstwert an. Dauer = grobe Schätzung für 4 Stunden Aufnahme.
STUFEN = [
    # ab MB, Modell,           Stapel, Ausrichtung, Sprecher, Dauer für 4 h (min)
    (10000, "large-v3",        16,     "cuda",      "cuda",   (15, 30)),
    (6000,  "large-v3",        8,      "cuda",      "cuda",   (20, 40)),
    (4500,  "large-v3",        4,      "cuda",      "cuda",   (30, 55)),
    (3500,  "large-v3-turbo",  4,      "cuda",      "cuda",   (20, 40)),
    (2000,  "large-v3-turbo",  2,      "cpu",       "cpu",    (60, 120)),
]
MIN_GRAFIK_MB = 2000   # darunter lohnt die Grafikkarte nicht – dann Prozessor
MIN_RAM_GB = 8
CPU_DAUER = (240, 600)  # 4 h Aufnahme auf dem Prozessor: 4–10 Stunden, je nach Prozessor


def profil(vram_mb: int | None, grenze_mb: int = 0, modell_wunsch: str = "auto", prozessor: bool = False) -> dict:
    """Wie der Worker arbeitet: Modell, Stapelgröße, Gerät je Schritt und Speichergrenze.

    vram_mb: Grafikspeicher der Karte (None = keine NVIDIA-Karte); grenze_mb: vom Nutzer erlaubter Anteil (0 = alles).
    """
    budget = min(vram_mb, grenze_mb) if (vram_mb and grenze_mb) else (vram_mb or 0)
    if prozessor or not vram_mb or budget < MIN_GRAFIK_MB:
        modell = modell_wunsch if modell_wunsch != "auto" else "large-v3-turbo"
        return {"stufe": "cpu", "modell": modell, "batch": 4, "genauigkeit": "int8", "geraet": "cpu",
                "ausrichten": "cpu", "sprecher": "cpu", "grenzeMb": 0, "budgetMb": 0,
                "dauerMin": CPU_DAUER if modell == "large-v3-turbo" else (CPU_DAUER[0] * 2, CPU_DAUER[1] * 2)}
    ab, modell, batch, ausrichten, sprecher, dauer = next(s for s in STUFEN if budget >= s[0])
    if modell_wunsch == "large-v3" and modell != "large-v3":
        modell, batch, dauer = "large-v3", max(1, batch // 2), (dauer[0] * 2, dauer[1] * 2)
        ausrichten = sprecher = "cpu"
    elif modell_wunsch == "large-v3-turbo" and modell != "large-v3-turbo":
        modell, dauer = "large-v3-turbo", (max(10, dauer[0] // 2), max(20, dauer[1] // 2))
    return {"stufe": f"gpu{ab}", "modell": modell, "batch": batch, "genauigkeit": "int8_float16", "geraet": "cuda",
            "ausrichten": ausrichten, "sprecher": sprecher, "grenzeMb": grenze_mb if grenze_mb else 0,
            "budgetMb": budget, "dauerMin": dauer}


def modell_fuer(vram_mb: int | None) -> dict:
    """Kurzform (ältere Aufrufe): Modell und Stapelgröße ohne Grenze."""
    p = profil(vram_mb)
    return {"modell": p["modell"], "batch": p["batch"]}


def arbeitsspeicher_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = Status()
            s.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.ullTotalPhys / 1024 ** 3
        with open("/proc/meminfo", encoding="ascii") as f:
            for zeile in f:
                if zeile.startswith("MemTotal:"):
                    return int(zeile.split()[1]) / 1024 ** 2
    except (OSError, ValueError, AttributeError):
        return None
    return None


def freier_platz_gb() -> float:
    return shutil.disk_usage(pfade.basis()).free / 1024 ** 3


def bericht() -> dict:
    karten = grafikkarten(frisch=True)
    k = karten[0] if karten else None
    platz = freier_platz_gb()
    ram = arbeitsspeicher_gb()
    return {
        "grafikkarte": asdict(k) if k else None,
        "grafikkarteNutzbar": bool(k and k.vram_mb >= MIN_GRAFIK_MB and k.treiber_ok),
        "treiberOk": k.treiber_ok if k else False,
        "minTreiber": MIN_TREIBER,
        "platzGb": round(platz, 1),
        "platzOk": platz >= PLATZ_KI_GB,
        "platzNoetigGb": PLATZ_KI_GB,
        "ramGb": round(ram, 1) if ram else None,
        "ramOk": ram is None or ram >= MIN_RAM_GB - 0.5,
        "minRamGb": MIN_RAM_GB,
        "kerne": os.cpu_count() or 0,
        "empfehlung": profil(k.vram_mb if k else None),
        "system": "windows" if sys.platform == "win32" else "linux",
    }
