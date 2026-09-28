"""Die Worker-App aktualisiert sich selbst – die Fassung und die Datei kommen vom eigenen Taleward-Server.

- Windows: Installer vom Server laden (/downloads/…), SHA-256 prüfen, still installieren (/VERYSILENT). Der
  Installer startet die App danach selbst wieder (--hintergrund). Keine Administratorrechte nötig.
- Linux (Installation über install-linux.sh / uv tool): „uv tool install“ mit dem Tag der Fassung, dann Neustart.
Installiert wird nur, wenn der Worker gerade nichts verarbeitet.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import httpx

from taleward_worker import VERSION, pfade


def neuer(a: str | None, b: str | None) -> bool:
    def tupel(v):
        try:
            return tuple(int(x) for x in (v or "").lstrip("v").split("-")[0].split("."))
        except ValueError:
            return None
    ta, tb = tupel(a), tupel(b)
    if not ta:
        return False
    if not tb:
        return True
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) > tb + (0,) * (n - len(tb))


def system() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def abfragen(server: str, token: str) -> dict | None:
    """Freigegebene Fassung der Worker-App auf dem Server; None = nichts Neueres."""
    try:
        r = httpx.get(f"{server.rstrip('/')}/worker/v1/app-update", params={"system": system()},
                      headers={"Authorization": f"Bearer {token}", "User-Agent": f"TalewardWorker/{VERSION}"},
                      timeout=20)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    return d if neuer(d.get("version"), VERSION) else None


def selbst_installierbar() -> bool:
    """Kann die App sich hier selbst aktualisieren? (gepackte Windows-App bzw. uv-tool-Installation unter Linux)"""
    if sys.platform == "win32":
        return bool(getattr(sys, "frozen", False))
    return "/uv/tools/" in sys.prefix.replace("\\", "/")


@dataclass
class Aktualisierung:
    angebot: dict
    server: str
    token: str
    phase: str = "bereit"   # bereit, laden, installieren, fertig, fehler
    anteil: float = 0.0
    fehler: str = ""
    _faden: threading.Thread | None = field(default=None, repr=False)

    def stand(self) -> dict:
        return {"version": self.angebot.get("version"), "notizen": self.angebot.get("notes"), "phase": self.phase,
                "anteil": round(self.anteil, 3), "fehler": self.fehler, "moeglich": selbst_installierbar()}

    def starten(self, vor_installation, beenden) -> None:
        """vor_installation(): Worker anhalten; beenden(): App schließen (Windows – der Installer startet sie neu)."""
        if self._faden and self._faden.is_alive():
            return
        self._faden = threading.Thread(target=self._lauf, args=(vor_installation, beenden), daemon=True,
                                       name="selbstupdate")
        self._faden.start()

    def _lauf(self, vor_installation, beenden) -> None:
        try:
            if sys.platform == "win32":
                datei = self._laden()
                self.phase = "installieren"
                vor_installation()
                flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen([str(datei), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                  "/CLOSEAPPLICATIONS", "/NOCANCEL"], creationflags=flags, close_fds=True)
                self.phase = "fertig"
                beenden()
            else:
                self.phase = "installieren"
                vor_installation()
                self._linux()
                self.phase = "fertig"
                programm = shutil.which("taleward-worker") or os.path.expanduser("~/.local/bin/taleward-worker")
                os.execv(programm, [programm, "--hintergrund"])  # durch die neue Fassung ersetzen
        except Exception as e:  # noqa: BLE001 – Fehler anzeigen, alte Fassung läuft weiter
            self.phase, self.fehler = "fehler", f"{type(e).__name__}: {e}"[:300]

    def _laden(self):
        url, soll = self.angebot.get("url"), (self.angebot.get("sha256") or "").lower()
        if not url or not soll:
            raise RuntimeError("Der Server nennt keine Datei oder Prüfsumme.")
        ordner = pfade.basis() / "updates"
        shutil.rmtree(ordner, ignore_errors=True)
        ordner.mkdir(parents=True)
        ziel = ordner / f"TalewardWorker-Setup-{self.angebot['version']}.exe"
        self.phase = "laden"
        h = hashlib.sha256()
        with httpx.stream("GET", url, headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(30, read=300),
                          follow_redirects=False) as r:
            r.raise_for_status()
            gesamt = int(r.headers.get("content-length") or self.angebot.get("sizeBytes") or 0)
            geladen = 0
            with ziel.open("wb") as f:
                for block in r.iter_bytes(1024 * 256):
                    h.update(block)
                    f.write(block)
                    geladen += len(block)
                    if gesamt:
                        self.anteil = min(0.99, geladen / gesamt)
        if h.hexdigest() != soll:
            ziel.unlink(missing_ok=True)
            raise RuntimeError("Prüfsumme stimmt nicht – Update verworfen.")
        return ziel

    def _linux(self) -> None:
        repo, tag = self.angebot.get("repo"), self.angebot.get("tag")
        if not repo or not tag:
            raise RuntimeError("Der Server nennt keine Quelle für Linux.")
        uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
        quelle = f"taleward-worker[qt] @ https://github.com/{repo}/archive/refs/tags/{tag}.zip#subdirectory=worker-app"
        erg = subprocess.run([uv, "tool", "install", "--force", "--python", "3.12", quelle],
                             capture_output=True, text=True, timeout=1800)
        if erg.returncode != 0:
            raise RuntimeError((erg.stderr or erg.stdout)[-300:])
