"""Symbol im Infobereich: Taleward-Zeichen mit farbigem Punkt für den Zustand."""
from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

from taleward_worker import pfade

FARBEN = {
    "warte": "#3d6a48",      # Salbei: bereit
    "arbeitet": "#c49235",   # Messing: arbeitet
    "startet": "#c49235",
    "abgestuerzt": "#c49235",
    "pausiert": "#8A7654",
    "gestoppt": "#8A7654",
    "getrennt": "#9e2a3a",   # Siegel: braucht Aufmerksamkeit
    "fehler": "#9e2a3a",
    "abgelehnt": "#9e2a3a",
}


@lru_cache(maxsize=16)
def symbol(art: str) -> Image.Image:
    basis = Image.open(pfade.ressourcen() / "ui" / "marke" / "icon-192.png").convert("RGBA").resize((64, 64))
    farbe = FARBEN.get(art)
    if farbe:
        d = ImageDraw.Draw(basis)
        d.ellipse((38, 38, 63, 63), fill="#FBF6EA")
        d.ellipse((42, 42, 59, 59), fill=farbe)
    return basis
