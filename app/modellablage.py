"""Modellablage des Servers: Das Sprechermodell liegt einmal auf dem Server und geht von dort an die Worker.

Warum: pyannote/speaker-diarization-community-1 steht unter CC-BY-4.0, ist bei Hugging Face aber „gated“ (Abruf nur
mit Konto und angegebenen Kontaktdaten). Bisher brauchte jeder Worker den Hugging-Face-Zugang des Betreibers. Jetzt:

1. Der Server holt das Modell einmal – vom Taleward-Spiegel (ohne Konto) oder mit seinem eigenen Hugging-Face-Zugang.
2. Worker laden es über das Worker-Protokoll vom Server (GET /worker/v1/models…). Sie brauchen kein Hugging-Face-Konto,
   und der Zugang des Betreibers bleibt auf dem Server.
3. Alle Worker nutzen genau dieselbe Fassung – Stimmabdrücke passen zusammen.

Sicherheit: Modelldateien von pyannote sind PyTorch-Checkpoints; beim Laden kann Code ausgeführt werden. Darum:
- Hugging Face: nur über HTTPS und mit fester Fassung (Commit), Prüfsummen der LFS-Dateien werden verglichen.
- Spiegel: nur, wenn die Prüfsumme seines Verzeichnisses (manifest.json) hier im Code steht (SPIEGEL). Ein Spiegel ohne
  eingetragene Prüfsumme wird nicht benutzt.
- Worker prüfen jede Datei gegen die Prüfsumme aus dem Server.

Namensnennung (CC-BY-4.0): Jede Ablage enthält NOTICE.txt mit Urheber, Lizenz, Quelle und „unverändert“.

Ablage: data/modellablage/<repo mit __>/<fassung>/{manifest.json, NOTICE.txt, Dateien…}; gemerkte Fassung in
server_meta modell.<repo>.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx
from sqlalchemy.orm import Session

from app.einstellungen import meta_lesen, meta_schreiben
from app.modelle import FASSUNGEN, SPRECHERMODELL

log = logging.getLogger("modellablage")

HF = "https://huggingface.co"
MAX_DATEI = 600 * 1024 ** 2  # einzelne Datei; das Sprechermodell hat zusammen etwa 30 MB
# Repo → (Fassung, SHA-256 der manifest.json). Wird eingetragen, sobald der Taleward-Spiegel steht.
SPIEGEL: dict[str, tuple[str, str]] = {}
NOTICE = {
    SPRECHERMODELL: ("pyannote speaker-diarization-community-1 – © pyannoteAI / Hervé Bredin and contributors.\n"
                     "License: Creative Commons Attribution 4.0 International (CC-BY-4.0), "
                     "https://creativecommons.org/licenses/by/4.0/\n"
                     "Source: https://huggingface.co/pyannote/speaker-diarization-community-1 (revision {fassung})\n"
                     "Redistributed unchanged by Taleward for use by its workers.\n"),
}


class AblageFehler(Exception):
    """Verständliche Meldung für Verwaltung und Kommandozeile."""


@dataclass
class Stand:
    repo: str
    fassung: str
    dateien: list[dict]  # {pfad, groesse, sha256}
    quelle: str  # huggingface | spiegel

    @property
    def groesse(self) -> int:
        return sum(d["groesse"] for d in self.dateien)

    def manifest(self) -> dict:
        return {"repo": self.repo, "fassung": self.fassung, "quelle": self.quelle, "dateien": self.dateien}


def ablage() -> Path:
    from app.config import get_settings

    return get_settings().data_dir / "modellablage"


def _ordner(repo: str, fassung: str) -> Path:
    return ablage() / repo.replace("/", "__") / fassung


def _sicherer_pfad(pfad: str) -> bool:
    p = PurePosixPath(pfad)
    return bool(pfad) and not p.is_absolute() and ".." not in p.parts and "\\" not in pfad and p.parts[0] != "."


def _sha256(datei: Path) -> str:
    h = hashlib.sha256()
    with datei.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- Lesen
def vorhanden(db: Session, repo: str = SPRECHERMODELL) -> Stand | None:
    fassung = FASSUNGEN.get(repo) or meta_lesen(db, f"modell.{repo}")
    if not fassung:
        return None
    try:
        m = json.loads((_ordner(repo, fassung) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Stand(repo, fassung, m["dateien"], m.get("quelle", "huggingface"))


def datei(db: Session, repo: str, fassung: str, pfad: str) -> Path | None:
    """Nur Dateien, die im Verzeichnis der aktuellen Fassung stehen (kein Pfad von außen)."""
    stand = vorhanden(db, repo)
    if stand is None or stand.fassung != fassung or not any(d["pfad"] == pfad for d in stand.dateien):
        return None
    p = _ordner(repo, fassung) / pfad
    return p if p.is_file() else None


# ---------------------------------------------------------------- Holen
def hf_token(db: Session) -> str | None:
    from app.config import get_settings

    return meta_lesen(db, "hf.token") or get_settings().hf_token or None


def _spiegel_url() -> str:
    from app.config import get_settings

    return (get_settings().model_mirror_url or "").rstrip("/")


def spiegel_nutzbar(repo: str = SPRECHERMODELL) -> bool:
    return bool(_spiegel_url()) and repo in SPIEGEL


def holen(db: Session, repo: str = SPRECHERMODELL, klient: httpx.Client | None = None) -> Stand:
    """Modell in fester Fassung auf den Server holen (Spiegel, sonst Hugging Face). Vorhandenes bleibt."""
    stand = vorhanden(db, repo)
    if stand is not None:
        return stand
    eigener = klient is None
    klient = klient or httpx.Client(timeout=httpx.Timeout(30, read=300), follow_redirects=True)
    try:
        if spiegel_nutzbar(repo) and (FASSUNGEN.get(repo) in (None, SPIEGEL[repo][0])):
            stand = _vom_spiegel(klient, repo)
        else:
            token = hf_token(db)
            if not token:
                raise AblageFehler("Für das Sprechermodell fehlt der Hugging-Face-Zugang (Verwaltung → Transkription), "
                                   "und ein Taleward-Spiegel ist noch nicht eingetragen.")
            stand = _von_hf(klient, repo, FASSUNGEN.get(repo) or meta_lesen(db, f"modell.{repo}"), token)
    finally:
        if eigener:
            klient.close()
    meta_schreiben(db, f"modell.{repo}", stand.fassung)
    db.commit()
    log.info("Modell %s@%s auf dem Server abgelegt (%s, %.1f MB).", repo, stand.fassung[:12], stand.quelle,
             stand.groesse / 1024 ** 2)
    return stand


def _laden(klient: httpx.Client, url: str, ziel: Path, kopf: dict, soll_sha: str | None) -> tuple[int, str]:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    h, n = hashlib.sha256(), 0
    with klient.stream("GET", url, headers=kopf) as r:
        if r.status_code in (401, 403):
            raise AblageFehler("Hugging Face verweigert den Zugang. Den Zugang prüfen und auf der Modellseite die "
                               "Bedingungen annehmen.")
        if r.status_code != 200:
            raise AblageFehler(f"Download fehlgeschlagen ({r.status_code}): {url}")
        with ziel.open("wb") as f:
            for block in r.iter_bytes(1024 * 1024):
                n += len(block)
                if n > MAX_DATEI:
                    raise AblageFehler(f"Datei zu groß: {url}")
                h.update(block)
                f.write(block)
    summe = h.hexdigest()
    if soll_sha and summe != soll_sha:
        raise AblageFehler(f"Prüfsumme stimmt nicht: {ziel.name}")
    return n, summe


def _ablegen(repo: str, fassung: str, quelle: str, arbeiten) -> Stand:
    """Erst in einen Zwischenordner, dann umbenennen – eine halb geladene Fassung gilt nie als vorhanden."""
    ziel = _ordner(repo, fassung)
    tmp = ziel.with_name(ziel.name + ".laden")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        dateien = arbeiten(tmp)
        if repo in NOTICE:
            (tmp / "NOTICE.txt").write_text(NOTICE[repo].format(fassung=fassung), encoding="utf-8")
        stand = Stand(repo, fassung, sorted(dateien, key=lambda d: d["pfad"]), quelle)
        (tmp / "manifest.json").write_text(json.dumps(stand.manifest(), indent=2), encoding="utf-8")
        shutil.rmtree(ziel, ignore_errors=True)
        tmp.rename(ziel)
        return stand
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _von_hf(klient: httpx.Client, repo: str, fassung: str | None, token: str) -> Stand:
    kopf = {"Authorization": f"Bearer {token}"}
    try:
        info = klient.get(f"{HF}/api/models/{repo}" + (f"/revision/{fassung}" if fassung else ""), headers=kopf)
    except httpx.HTTPError as e:
        raise AblageFehler(f"Hugging Face nicht erreichbar ({e.__class__.__name__}).") from None
    if info.status_code in (401, 403):
        raise AblageFehler("Hugging Face verweigert den Zugang. Den Zugang prüfen und auf der Modellseite die "
                           "Bedingungen annehmen.")
    if info.status_code != 200:
        raise AblageFehler(f"Modell {repo} bei Hugging Face nicht gefunden ({info.status_code}).")
    fassung = info.json()["sha"]
    baum = klient.get(f"{HF}/api/models/{repo}/tree/{fassung}", params={"recursive": "true"}, headers=kopf)
    if baum.status_code != 200:
        raise AblageFehler(f"Dateiliste von {repo} nicht lesbar ({baum.status_code}).")
    eintraege = [e for e in baum.json() if e.get("type") == "file" and _sicherer_pfad(e.get("path", ""))]

    def arbeiten(tmp: Path) -> list[dict]:
        dateien = []
        for e in eintraege:
            soll = (e.get("lfs") or {}).get("oid")  # LFS: SHA-256 des Inhalts
            n, summe = _laden(klient, f"{HF}/{repo}/resolve/{fassung}/{e['path']}", tmp / e["path"], kopf, soll)
            dateien.append({"pfad": e["path"], "groesse": n, "sha256": summe})
        return dateien

    return _ablegen(repo, fassung, "huggingface", arbeiten)


def _vom_spiegel(klient: httpx.Client, repo: str) -> Stand:
    fassung, manifest_sha = SPIEGEL[repo]
    basis = f"{_spiegel_url()}/{repo}/{fassung}"
    try:
        r = klient.get(f"{basis}/manifest.json")
    except httpx.HTTPError as e:
        raise AblageFehler(f"Taleward-Spiegel nicht erreichbar ({e.__class__.__name__}).") from None
    if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != manifest_sha:
        raise AblageFehler("Das Verzeichnis des Taleward-Spiegels passt nicht zur erwarteten Prüfsumme.")
    m = r.json()
    if m.get("repo") != repo or m.get("fassung") != fassung:
        raise AblageFehler("Der Taleward-Spiegel liefert eine andere Fassung als erwartet.")

    def arbeiten(tmp: Path) -> list[dict]:
        dateien = []
        for d in m["dateien"]:
            if not _sicherer_pfad(d["pfad"]):
                raise AblageFehler(f"Unerwarteter Pfad im Spiegel: {d['pfad']}")
            n, summe = _laden(klient, f"{basis}/{d['pfad']}", tmp / d["pfad"], {}, d["sha256"])
            dateien.append({"pfad": d["pfad"], "groesse": n, "sha256": summe})
        return dateien

    return _ablegen(repo, fassung, "spiegel", arbeiten)


# ---------------------------------------------------------------- Spiegel erstellen (für das Taleward-Projekt)
def spiegel_erstellen(db: Session, ziel: Path, repo: str = SPRECHERMODELL) -> tuple[Path, str]:
    """Die Ablage als statisches Verzeichnis für einen Webserver kopieren: <ziel>/<repo>/<fassung>/…
    Liefert den Ordner und die SHA-256 der manifest.json (für SPIEGEL im Code)."""
    stand = vorhanden(db, repo)
    if stand is None:
        raise AblageFehler("Das Modell liegt noch nicht auf diesem Server (erst holen).")
    quelle = _ordner(repo, stand.fassung)
    aus = ziel / repo / stand.fassung
    shutil.rmtree(aus, ignore_errors=True)
    shutil.copytree(quelle, aus)
    manifest = {"repo": repo, "fassung": stand.fassung, "dateien": stand.dateien}
    inhalt = json.dumps(manifest, indent=2).encode()
    (aus / "manifest.json").write_bytes(inhalt)
    return aus, hashlib.sha256(inhalt).hexdigest()


# ---------------------------------------------------------------- Automatisch (aus der Wartung)
def gebraucht(db: Session) -> bool:
    """Sprechermodell wird gebraucht, sobald lokal transkribiert wird oder ein Worker existiert."""
    from sqlalchemy import func, select

    from app.einrichtung import betriebsart
    from app.models import Worker

    return betriebsart(db) in ("lokal", "beides") or (db.scalar(
        select(func.count()).select_from(Worker).where(Worker.revoked_at.is_(None))) or 0) > 0


def automatisch(db: Session, klient: httpx.Client | None = None) -> Stand | None:
    """Holen, wenn gebraucht, noch nicht da und eine Quelle eingerichtet ist. Nach einem Fehlschlag frühestens
    nach einer Stunde erneut (oder sofort über „Jetzt laden“)."""
    from datetime import datetime, timedelta, timezone

    if vorhanden(db) is not None or not gebraucht(db) or not (hf_token(db) or spiegel_nutzbar()):
        return None
    letzter = meta_lesen(db, "modellablage.versuch")
    if letzter:
        z = datetime.fromisoformat(letzter)
        if datetime.now(timezone.utc) - (z if z.tzinfo else z.replace(tzinfo=timezone.utc)) < timedelta(hours=1):
            return None
    return versuchen(db, klient)


def versuchen(db: Session, klient: httpx.Client | None = None) -> Stand | None:
    from datetime import datetime, timezone

    meta_schreiben(db, "modellablage.versuch", datetime.now(timezone.utc).isoformat())
    db.commit()
    try:
        stand = holen(db, klient=klient)
    except AblageFehler as e:
        meta_schreiben(db, "modellablage.fehler", str(e)[:500])
        db.commit()
        log.warning("Sprechermodell nicht geladen: %s", e)
        return None
    meta_schreiben(db, "modellablage.fehler", "")
    db.commit()
    return stand
