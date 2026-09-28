"""Updates für App, Worker und Server – der Server ist die einzige Stelle, die bei GitHub nachfragt.

- Einmal am Tag (und auf Knopfdruck in der Verwaltung) liest der Server die Releases bei GitHub:
  App (Android-APK), Worker (Windows-Installer; Linux ohne Datei) und die eigenen Server-Fassungen (Git-Tags).
- Dateien für App und Worker lädt er einmal herunter, prüft die SHA-256 (sofern GitHub sie nennt) und bietet sie unter
  /downloads/… selbst an. Handys und Worker fragen nur ihren Server – nie GitHub.
- Freigabe: „automatisch“ (Standard) gibt jede neue Fassung sofort frei; „manuell“ erst nach Klick in der Verwaltung.
  Freigeben heißt nur „dieser Server verteilt die Fassung“ – er verbietet keine neueren Apps (dafür gibt es nur die
  Mindestversion, 426). Hat eine App mehrere Server, nimmt sie die neueste Fassung, die einer davon anbietet.

server_meta:
  update.stand.<art>        JSON der neuesten gefundenen Fassung (version, notizen, datei, sha256, groesse, url …)
  update.freigegeben.<art>  freigegebene Fassung
  update.modus              automatisch | manuell
  update.geprueft           Zeitpunkt der letzten Prüfung (ISO), update.fehler = letzter Fehler
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as paket_version
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben, version_tupel

log = logging.getLogger("aktualisierung")
TAEGLICH = timedelta(hours=24)
ERNEUT_NACH_FEHLER = timedelta(hours=2)
BEHALTEN = 2  # so viele Fassungen je Art bleiben im Zwischenspeicher


@dataclass(frozen=True)
class Art:
    schluessel: str
    name: str
    tag: str             # regulärer Ausdruck, Gruppe 1 = Fassung
    datei: str | None    # regulärer Ausdruck für den Anhang; None = nur die Fassung (Linux-Worker, Server)
    quelle: str          # "releases" oder "tags"

    def repo(self) -> str:
        s = get_settings()
        return {"app": s.update_app_repo, "server": s.update_server_repo}.get(self.schluessel, s.update_worker_repo)


ARTEN = {a.schluessel: a for a in [
    Art("app", "App (Android)", r"^(?:app-)?v?(\d+\.\d+\.\d+)$", r"\.apk$", "releases"),
    Art("worker-windows", "Worker (Windows)", r"^worker-v(\d+\.\d+\.\d+)$", r"^TalewardWorker-Setup\.exe$", "releases"),
    Art("worker-linux", "Worker (Linux)", r"^worker-v(\d+\.\d+\.\d+)$", None, "releases"),
    Art("server", "Server", r"^v(\d+\.\d+\.\d+)$", None, "tags"),
]}


class UpdateFehler(Exception):
    pass


def eigene_fassung() -> str:
    try:
        return paket_version("taleward-server")
    except PackageNotFoundError:
        return "0.0.0"


def ablage() -> Path:
    return get_settings().data_dir / "aktualisierung"


def _neuer(a: str | None, b: str | None) -> bool:
    ta, tb = version_tupel(a), version_tupel(b)
    if ta is None:
        return False
    if tb is None:
        return True
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) > tb + (0,) * (n - len(tb))


def _klient() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30, read=300), follow_redirects=True,
                        headers={"Accept": "application/vnd.github+json", "User-Agent": "Taleward-Server"})


# ---------------------------------------------------------------- Stand lesen/schreiben
def stand(db: Session, art: str) -> dict | None:
    wert = meta_lesen(db, f"update.stand.{art}")
    try:
        return json.loads(wert) if wert else None
    except ValueError:
        return None


def modus(db: Session) -> str:
    return "manuell" if meta_lesen(db, "update.modus") == "manuell" else "automatisch"


def modus_setzen(db: Session, neu: str) -> None:
    meta_schreiben(db, "update.modus", "manuell" if neu == "manuell" else "automatisch")
    if neu != "manuell":  # beim Umschalten auf automatisch alles Vorhandene freigeben
        for art in ARTEN:
            s = stand(db, art)
            if s and s.get("bereit"):
                meta_schreiben(db, f"update.freigegeben.{art}", s["version"])
    db.commit()


def freigegeben(db: Session, art: str) -> dict | None:
    """Stand der freigegebenen Fassung (nur, wenn die Datei – falls nötig – bereitliegt)."""
    v = meta_lesen(db, f"update.freigegeben.{art}")
    s = stand(db, art)
    if not v or not s:
        return None
    alt = json.loads(meta_lesen(db, f"update.fassung.{art}.{v}") or "null") if s.get("version") != v else s
    if not alt or (ARTEN[art].datei and not datei(art, alt["version"], alt.get("datei"))):
        return None
    return alt


def freigeben(db: Session, art: str, version: str) -> bool:
    s = stand(db, art)
    if not s or s.get("version") != version or not s.get("bereit"):
        return False
    meta_schreiben(db, f"update.freigegeben.{art}", version)
    db.commit()
    return True


def datei(art: str, version: str, name: str | None) -> Path | None:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    pfad = ablage() / art / version / name
    return pfad if pfad.is_file() else None


# ---------------------------------------------------------------- GitHub lesen
def _neueste(klient: httpx.Client, art: Art) -> dict | None:
    repo = art.repo()
    if art.quelle == "tags":
        r = klient.get(f"https://api.github.com/repos/{repo}/tags", params={"per_page": 100})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        treffer = [(m.group(1), t) for t in r.json() if (m := re.match(art.tag, t.get("name", "")))]
        if not treffer:
            return None
        v, _ = max(treffer, key=lambda x: version_tupel(x[0]) or (0,))
        return {"version": v, "notizen": "", "url": f"https://github.com/{repo}/releases/tag/v{v}"}
    r = klient.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 30})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    beste = None
    for rel in r.json():
        m = re.match(art.tag, rel.get("tag_name", ""))
        if not m or rel.get("draft") or rel.get("prerelease"):
            continue
        anhang = None
        if art.datei:
            anhang = next((a for a in rel.get("assets", []) if re.search(art.datei, a.get("name", ""))), None)
            if anhang is None:
                continue  # Release ohne passende Datei (z. B. noch im Bau) überspringen
        eintrag = {"version": m.group(1), "notizen": (rel.get("body") or "").strip()[:4000],
                   "url": rel.get("html_url"), "veroeffentlicht": rel.get("published_at"), "tag": rel.get("tag_name")}
        if anhang:
            digest = anhang.get("digest") or ""
            eintrag.update(datei=anhang["name"], groesse=anhang.get("size"), quelle=anhang["browser_download_url"],
                           sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else None)
        if beste is None or _neuer(eintrag["version"], beste["version"]):
            beste = eintrag
    return beste


def _laden(klient: httpx.Client, art: str, eintrag: dict) -> dict:
    """Datei herunterladen und prüfen; liegt sie schon da, nur prüfen."""
    ziel_ordner = ablage() / art / eintrag["version"]
    ziel = ziel_ordner / eintrag["datei"]
    if ziel.is_file() and (not eintrag.get("sha256") or _sha(ziel) == eintrag["sha256"]):
        return {**eintrag, "sha256": _sha(ziel), "groesse": ziel.stat().st_size, "bereit": True}
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(ziel.suffix + ".teil")
    h = hashlib.sha256()
    with klient.stream("GET", eintrag["quelle"], headers={"Accept": "application/octet-stream"}) as r:
        if r.status_code != 200:
            raise UpdateFehler(f"{eintrag['datei']}: HTTP {r.status_code}")
        with tmp.open("wb") as f:
            for block in r.iter_bytes(1024 * 1024):
                h.update(block)
                f.write(block)
    summe = h.hexdigest()
    if eintrag.get("sha256") and summe != eintrag["sha256"]:
        tmp.unlink(missing_ok=True)
        raise UpdateFehler(f"{eintrag['datei']}: Prüfsumme stimmt nicht")
    tmp.replace(ziel)
    return {**eintrag, "sha256": summe, "groesse": ziel.stat().st_size, "bereit": True}


def _sha(pfad: Path) -> str:
    h = hashlib.sha256()
    with pfad.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _aufraeumen(db: Session, art: str) -> None:
    """Nur die neuesten Fassungen und die freigegebene behalten."""
    ordner = ablage() / art
    if not ordner.is_dir():
        return
    behalten = {meta_lesen(db, f"update.freigegeben.{art}")}
    alle = sorted((p for p in ordner.iterdir() if p.is_dir()), key=lambda p: version_tupel(p.name) or (0,), reverse=True)
    behalten |= {p.name for p in alle[:BEHALTEN]}
    for p in alle:
        if p.name not in behalten:
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------- Prüfen
def pruefen(db: Session, klient: httpx.Client | None = None) -> dict:
    """Alle Arten prüfen. Liefert {art: version|None}. Fehler je Art werden gesammelt, nicht geworfen."""
    eigener = klient is None
    klient = klient or _klient()
    ergebnis, fehler = {}, []
    try:
        for schluessel, art in ARTEN.items():
            try:
                eintrag = _neueste(klient, art)
            except (httpx.HTTPError, ValueError) as e:
                fehler.append(f"{art.name}: {type(e).__name__}")
                continue
            ergebnis[schluessel] = eintrag["version"] if eintrag else None
            if not eintrag:
                continue
            bisher = stand(db, schluessel)
            if bisher and bisher.get("version") == eintrag["version"] and bisher.get("bereit"):
                continue
            if art.datei:
                try:
                    eintrag = _laden(klient, schluessel, eintrag)
                except (httpx.HTTPError, UpdateFehler, OSError) as e:
                    fehler.append(f"{art.name}: {e}")
                    continue
            else:
                eintrag["bereit"] = True
            if bisher and bisher.get("version") != eintrag["version"]:
                meta_schreiben(db, f"update.fassung.{schluessel}.{bisher['version']}", json.dumps(bisher))
            meta_schreiben(db, f"update.stand.{schluessel}", json.dumps(eintrag))
            if modus(db) == "automatisch" or not meta_lesen(db, f"update.freigegeben.{schluessel}"):
                # Erste gefundene Fassung ist immer freigegeben – sonst gäbe es im manuellen Modus gar nichts
                meta_schreiben(db, f"update.freigegeben.{schluessel}", eintrag["version"])
            db.commit()
            _aufraeumen(db, schluessel)
    finally:
        if eigener:
            klient.close()
    meta_schreiben(db, "update.geprueft", utcnow().isoformat())
    meta_schreiben(db, "update.fehler", "; ".join(fehler))
    db.commit()
    if fehler:
        log.warning("Update-Prüfung: %s", "; ".join(fehler))
    _server_melden(db)
    return ergebnis


def automatisch(db: Session) -> None:
    """Aus der Wartung: einmal am Tag (nach einem Fehler nach 2 Stunden erneut)."""
    if not get_settings().update_check:
        return
    zuletzt = meta_lesen(db, "update.geprueft")
    if zuletzt:
        z = datetime.fromisoformat(zuletzt)
        z = z if z.tzinfo else z.replace(tzinfo=timezone.utc)
        warten = ERNEUT_NACH_FEHLER if meta_lesen(db, "update.fehler") else TAEGLICH
        if utcnow() - z < warten:
            return
    pruefen(db)


def server_update(db: Session) -> dict | None:
    """Neuere Server-Fassung bei GitHub (Tag), sonst None."""
    s = stand(db, "server")
    return s if s and _neuer(s.get("version"), eigene_fassung()) else None


def _server_melden(db: Session) -> None:
    """Einmal je neuer Server-Fassung Bescheid geben (ntfy/E-Mail, falls eingerichtet)."""
    neu = server_update(db)
    if not neu or meta_lesen(db, "update.gemeldet.server") == neu["version"]:
        return
    meta_schreiben(db, "update.gemeldet.server", neu["version"])
    db.commit()
    try:
        from app import benachrichtigung

        benachrichtigung.melden(db, "server_update", wichtig=False, version=neu["version"], jetzt=eigene_fassung())
    except Exception:  # noqa: BLE001 – Benachrichtigung ist Beiwerk
        log.exception("Hinweis auf neue Server-Fassung nicht gesendet")


# ---------------------------------------------------------------- Für App, Worker, Verwaltung
def download_pfad(art: str, s: dict) -> str:
    return f"/downloads/{art}/{s['version']}/{s['datei']}"


def angebot(db: Session, art: str, basis: str) -> dict | None:
    """Freigegebene Fassung mit Download-Adresse auf diesem Server (für App-Info und Worker)."""
    s = freigegeben(db, art)
    if not s:
        return None
    aus = {"version": s["version"], "notes": s.get("notizen") or None, "tag": s.get("tag")}
    if ARTEN[art].datei:
        aus.update(url=basis.rstrip("/") + download_pfad(art, s), sha256=s.get("sha256"), sizeBytes=s.get("groesse"))
    return aus


def uebersicht(db: Session) -> list[dict]:
    zeilen = []
    for schluessel, art in ARTEN.items():
        if schluessel == "server":
            continue
        s = stand(db, schluessel)
        zeilen.append({"art": schluessel, "name": art.name, "neueste": s, "freigegeben":
                       meta_lesen(db, f"update.freigegeben.{schluessel}"), "repo": art.repo()})
    return zeilen
