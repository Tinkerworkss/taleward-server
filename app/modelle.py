"""Feste Modellfassungen für den Worker.

Die KI-Pakete sind in uv.lock festgelegt. Die Modelldateien kommen von Hugging Face. Ohne Festlegung fragen
whisperx und pyannote bei jedem Laden nach einer neueren Fassung und übernehmen sie still. Das darf nicht sein:
Stimmabdrücke passen nur zu genau der Fassung des Sprechermodells, mit der sie entstanden sind.

Welche Fassung gilt:
1. Die Fassung hier im Code (FASSUNGEN). Sie gilt für alle Installationen und ändert sich nur mit einem geprüften
   Server-Update.
2. Sonst die Fassung, die dieser Worker beim ersten Laden gemerkt hat (data/modelle.json).
3. Sonst einmalig die vorhandene bzw. aktuelle Fassung nehmen und merken.

Geladen wird danach aus dem lokalen Ordner, ohne Nachfrage bei Hugging Face.

Das Sprechermodell holt der Worker vom Server (app/modellablage.py), sobald es dort liegt: ohne eigenes
Hugging-Face-Konto, in der Fassung des Servers, jede Datei mit SHA-256 geprüft. Nur wenn der Server es (noch) nicht hat,
lädt der Worker selbst von Hugging Face.
Nicht betroffen: das deutsche und englische Ausrichtungsmodell (torchaudio) und die Spracherkennung (VAD) –
beide hängen fest an den Paketversionen.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SPRECHERMODELL = "pyannote/speaker-diarization-community-1"

# Hugging-Face-Kennung → Commit. Eingetragen wird nur, was auf echter Hardware geprüft ist.
FASSUNGEN: dict[str, str] = {}

# Was im lokalen Ordner liegen muss, damit er als vollständig gilt
PRUEFDATEI = {SPRECHERMODELL: "config.yaml"}
WHISPER_DATEIEN = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


class ModellFehler(Exception):
    """Modell nicht ladbar – verständliche Meldung."""


@dataclass
class Bereit:
    repo: str
    fassung: str | None  # Commit; None bei einem eigenen Ordner
    pfad: Path
    quelle: str  # festgelegt | gemerkt | neu | ordner

    @property
    def kennung(self) -> str:
        return f"{self.repo}@{self.fassung[:12]}" if self.fassung else self.repo


def merkdatei() -> Path:
    from app.config import get_settings

    return get_settings().data_dir / "modelle.json"


def gemerkt() -> dict[str, str]:
    try:
        daten = json.loads(merkdatei().read_text(encoding="utf-8"))
        return {k: v for k, v in daten.items() if isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def _merken(repo: str, fassung: str) -> None:
    daten = gemerkt()
    if daten.get(repo) == fassung:
        return
    daten[repo] = fassung
    datei = merkdatei()
    datei.parent.mkdir(parents=True, exist_ok=True)
    tmp = datei.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(datei)


def whisper_repo(name: str) -> str:
    """„large-v3“ → „Systran/faster-whisper-large-v3“ (gleiche Zuordnung wie faster-whisper)."""
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:  # ohne KI-Pakete (Tests)
        _MODELS = {"large-v3": "Systran/faster-whisper-large-v3"}
    return _MODELS.get(name, name)


def _vollstaendig(pfad: Path, repo: str) -> bool:
    pruef = PRUEFDATEI.get(repo, "model.bin" if "whisper" in repo else None)
    return pfad.is_dir() and (pruef is None or (pfad / pruef).exists())


def _laden(repo: str, fassung: str | None, token: str | None, nur_lokal: bool) -> Path:
    from huggingface_hub import snapshot_download

    muster = WHISPER_DATEIEN if "whisper" in repo else None
    return Path(snapshot_download(repo, revision=fassung, token=token, local_files_only=nur_lokal,
                                  allow_patterns=muster))


def bereitstellen(repo_oder_ordner: str, token: str | None = None) -> Bereit:
    """Pfad zur festen Fassung eines Modells; lädt nur, wenn sie noch nicht auf diesem Worker liegt."""
    eigener = Path(repo_oder_ordner).expanduser()
    if eigener.is_dir():
        return Bereit(repo_oder_ordner, None, eigener, "ordner")
    repo = repo_oder_ordner
    soll = FASSUNGEN.get(repo)
    quelle = "festgelegt"
    if soll is None:
        soll, quelle = gemerkt().get(repo), "gemerkt"
    pfad = None
    try:  # zuerst lokal – ohne Netz; ohne Fassung: das, was schon im Zwischenspeicher liegt
        pfad = _laden(repo, soll, token, nur_lokal=True)
        if not _vollstaendig(pfad, repo):
            pfad = None
    except Exception:  # noqa: BLE001 – nicht vorhanden → laden
        pfad = None
    if pfad is None:
        log.info("Lade Modell %s%s (einmalig, kann dauern) …", repo, f" in Fassung {soll[:12]}" if soll else "")
        try:
            pfad = _laden(repo, soll, token, nur_lokal=False)
        except Exception as e:  # noqa: BLE001
            raise ModellFehler(_meldung(repo, e)) from e
    fassung = pfad.name  # Ordnername im Zwischenspeicher = Commit
    if soll is None:
        quelle = "neu"
    elif fassung != soll:
        raise ModellFehler(f"Modell {repo}: erwartet Fassung {soll[:12]}, gefunden {fassung[:12]}.")
    _merken(repo, fassung)
    return Bereit(repo, fassung, pfad, quelle)


# ---------------------------------------------------------------- Vom Server (Worker-Protokoll)
class ServerQuelle:
    """Lädt Modelle, die der Server verteilt (GET /worker/v1/models…), mit dem Worker-Token."""

    def __init__(self, server: str, token: str, klient=None):
        import httpx

        self.server = server.rstrip("/")
        self.kopf = {"Authorization": f"Bearer {token}"}
        self.klient = klient or httpx.Client(timeout=httpx.Timeout(30, read=300))

    def verzeichnis(self, repo: str) -> dict | None:
        r = self.klient.get(f"{self.server}/worker/v1/models", params={"repo": repo}, headers=self.kopf)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ModellFehler(f"Server liefert das Modell {repo} nicht ({r.status_code}).")
        return r.json()

    def laden(self, repo: str, fassung: str, pfad: str, ziel: Path) -> str:
        import hashlib

        ziel.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with self.klient.stream("GET", f"{self.server}/worker/v1/models/file", headers=self.kopf,
                                params={"repo": repo, "fassung": fassung, "pfad": pfad}) as r:
            if r.status_code != 200:
                raise ModellFehler(f"Modelldatei {pfad} nicht vom Server ladbar ({r.status_code}).")
            with ziel.open("wb") as f:
                for block in r.iter_bytes(1024 * 1024):
                    h.update(block)
                    f.write(block)
        return h.hexdigest()


def _sicher(pfad: str) -> bool:
    from pathlib import PurePosixPath

    p = PurePosixPath(pfad)
    return bool(pfad) and not p.is_absolute() and ".." not in p.parts and "\\" not in pfad


def vom_server(repo: str, quelle: ServerQuelle) -> Bereit | None:
    """Feste Fassung des Servers bereitstellen (aus dem lokalen Zwischenspeicher oder frisch geladen).
    None, wenn der Server das Modell nicht hat."""
    import shutil

    from app.config import get_settings

    try:
        v = quelle.verzeichnis(repo)
    except ModellFehler:
        raise
    except Exception as e:  # noqa: BLE001 – Netz
        # Server gerade nicht erreichbar (WLAN beim Anmelden noch nicht da, Server kurz weg): liegt die zuletzt
        # gemerkte Fassung vollständig hier, arbeiten wir damit weiter – sonst müsste der Worker 10 Minuten warten.
        vorhanden = _vorhanden(repo)
        if vorhanden is not None:
            log.warning("Server nicht erreichbar (%s) – nehme das gemerkte Modell %s@%s",
                        type(e).__name__, repo, vorhanden.fassung[:12])
            return vorhanden
        raise ModellFehler(f"Server nicht erreichbar, um das Modell {repo} zu laden ({type(e).__name__}).") from e
    if v is None:
        return None
    fassung = v["fassung"]
    fest = FASSUNGEN.get(repo)
    if fest and fest != fassung:
        raise ModellFehler(f"Modell {repo}: Der Server hat Fassung {fassung[:12]}, dieser Worker erwartet "
                           f"{fest[:12]}. Server und Worker auf dieselbe Version aktualisieren.")
    ordner = get_settings().data_dir / "modelle" / repo.replace("/", "__") / fassung
    fertig = ordner / ".vollstaendig"
    if not fertig.exists():
        log.info("Lade Modell %s@%s vom Server (einmalig) …", repo, fassung[:12])
        tmp = ordner.with_name(ordner.name + ".laden")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            for d in v["dateien"]:
                if not _sicher(d["pfad"]):
                    raise ModellFehler(f"Unerwarteter Pfad im Modellverzeichnis: {d['pfad']}")
                if quelle.laden(repo, fassung, d["pfad"], tmp / d["pfad"]) != d["sha256"]:
                    raise ModellFehler(f"Prüfsumme stimmt nicht: {d['pfad']} – bitte erneut versuchen.")
            (tmp / ".vollstaendig").write_text(fassung, encoding="utf-8")
            shutil.rmtree(ordner, ignore_errors=True)
            tmp.rename(ordner)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    _merken(repo, fassung)
    return Bereit(repo, fassung, ordner, "server")


def _vorhanden(repo: str) -> Bereit | None:
    """Die gemerkte Fassung eines Servermodells, wenn sie vollständig auf der Platte liegt."""
    from app.config import get_settings

    fassung = gemerkt().get(repo)
    if not fassung:
        return None
    ordner = get_settings().data_dir / "modelle" / repo.replace("/", "__") / fassung
    if not (ordner / ".vollstaendig").exists():
        return None
    return Bereit(repo, fassung, ordner, "gemerkt")


def _meldung(repo: str, e: Exception) -> str:
    text = f"{type(e).__name__}: {e}"
    if any(w in text for w in ("401", "403", "Gated", "gated", "Unauthorized")):
        return (f"Kein Zugang zu {repo} bei Hugging Face. HF_TOKEN in der .env prüfen und die Modellbedingungen "
                "auf der Modellseite annehmen (docs/BETRIEB.md, „Transkription“).")
    if any(w in text for w in ("Connection", "Timeout", "resolve", "Offline", "offline")):
        return f"Modell {repo} liegt noch nicht auf diesem Computer, und Hugging Face ist nicht erreichbar."
    return f"Modell {repo} konnte nicht geladen werden ({text[:300]})."


def passt(a: str | None, b: str | None) -> bool:
    """Passen zwei Stimmabdrücke zusammen? Gleiches Modell; die Fassung zählt nur, wenn beide sie kennen
    (Abdrücke von vor der Festlegung haben keine – sie stammen praktisch immer aus derselben Fassung)."""
    if not a or not b:
        return True
    ra, _, fa = a.partition("@")
    rb, _, fb = b.partition("@")
    return ra == rb and (not fa or not fb or fa == fb)
