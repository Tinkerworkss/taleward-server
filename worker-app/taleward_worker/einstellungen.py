"""Einstellungen der App (JSON im Benutzerordner). Der Worker-Schlüssel steht nur hier und ist nur für den
Benutzer lesbar."""
from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field, fields

from taleward_worker import pfade


@dataclass
class Einstellungen:
    server: str = ""
    token: str = ""
    name: str = field(default_factory=socket.gethostname)
    unsicher: bool = False          # HTTP im Heimnetz erlaubt
    testmodus: bool = False         # ohne KI (Platzhaltertext) – zum Ausprobieren ohne Grafikkarte
    modell: str = "auto"            # auto | large-v3 | large-v3-turbo
    geraet: str = "auto"            # auto (Grafikkarte, wenn nutzbar) | cpu (nur Prozessor)
    vram_grenze_mb: int = 0         # höchstens so viel Grafikspeicher für Taleward (0 = alles)
    autostart: bool = False
    im_hintergrund: bool = True     # Fenster schließen = weiterlaufen (Symbol im Infobereich)
    auto_update: bool = True        # neue Fassungen der App selbst installieren, sobald der Worker frei ist
    pausiert: bool = False
    motor_fassung: str = ""         # installierte Fassung des KI-Pakets (= Serverfassung)
    motor_art: str = ""             # "ki" oder "test"
    sprache: str = ""               # leer = Systemsprache
    hinweis_hintergrund_gezeigt: bool = False

    @property
    def gekoppelt(self) -> bool:
        return bool(self.server and self.token)

    def speichern(self) -> None:
        ziel = pfade.einstellungen()
        tmp = ziel.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, ziel)

    @classmethod
    def laden(cls) -> "Einstellungen":
        try:
            daten = json.loads(pfade.einstellungen().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        bekannt = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in daten.items() if k in bekannt})

    def fuer_oberflaeche(self) -> dict:
        d = asdict(self)
        d.pop("token")
        d["gekoppelt"] = self.gekoppelt
        return d
