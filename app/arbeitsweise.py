"""Wie ein Worker arbeitet: Modell, Stapelgröße und Gerät je Schritt – passend zum Grafikspeicher.

Dieselben Stufen wie in der Worker-App (taleward-worker, hardware.py). Hier für Worker ohne Oberfläche, z. B. den
eingebauten Worker im Docker-Paket („chronik worker --automatisch“).
"""
from __future__ import annotations

import shutil
import subprocess

# Stufen nach verfügbarem Grafikspeicher (MB). Gemessen: large-v3 mit Stapel 8 braucht auf der RTX 3060 Ti höchstens
# ~5,5 GB. Die übrigen Werte sind vorsichtige Schätzungen.
STUFEN = [
    # ab MB, Modell,          Stapel, Ausrichtung, Sprecher
    (10000, "large-v3",       16,     "cuda",      "cuda"),
    (6000,  "large-v3",       8,      "cuda",      "cuda"),
    (4500,  "large-v3",       4,      "cuda",      "cuda"),
    (3500,  "large-v3-turbo", 4,      "cuda",      "cuda"),
    (2000,  "large-v3-turbo", 2,      "cpu",       "cpu"),
]
MIN_GRAFIK_MB = 2000  # darunter lohnt die Grafikkarte nicht – dann Prozessor


def profil(vram_mb: int | None, grenze_mb: int = 0, modell_wunsch: str = "auto", prozessor: bool = False) -> dict:
    """vram_mb: Grafikspeicher der Karte (None = keine NVIDIA-Karte); grenze_mb: erlaubter Anteil (0 = alles)."""
    budget = min(vram_mb, grenze_mb) if (vram_mb and grenze_mb) else (vram_mb or 0)
    if prozessor or not vram_mb or budget < MIN_GRAFIK_MB:
        modell = modell_wunsch if modell_wunsch in ("large-v3", "large-v3-turbo") else "large-v3-turbo"
        return {"stufe": "cpu", "modell": modell, "batch": 4, "genauigkeit": "int8", "geraet": "cpu",
                "ausrichten": "cpu", "sprecher": "cpu", "grenzeMb": 0}
    ab, modell, batch, ausrichten, sprecher = next(s for s in STUFEN if budget >= s[0])
    if modell_wunsch == "large-v3" and modell != "large-v3":
        modell, batch, ausrichten, sprecher = "large-v3", max(1, batch // 2), "cpu", "cpu"
    elif modell_wunsch == "large-v3-turbo":
        modell = "large-v3-turbo"
    return {"stufe": f"gpu{ab}", "modell": modell, "batch": batch, "genauigkeit": "int8_float16", "geraet": "cuda",
            "ausrichten": ausrichten, "sprecher": sprecher, "grenzeMb": grenze_mb or 0}


def grafikspeicher_mb() -> int | None:
    """Grafikspeicher der ersten NVIDIA-Karte (nvidia-smi, sonst PyTorch); None ohne nutzbare Karte."""
    programm = shutil.which("nvidia-smi")
    if programm:
        try:
            aus = subprocess.run([programm, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
            if aus:
                return int(float(aus[0].strip()))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.get_device_properties(0).total_memory / 2 ** 20)
    except Exception:  # noqa: BLE001 – ohne KI-Paket oder Treiber: keine Karte
        return None
    return None
