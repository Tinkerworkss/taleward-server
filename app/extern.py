"""Externe Transkription als Ersatz (Mistral Voxtral) – läuft in der Zentrale.

Bewusst hohe Schwelle, weil das Audio dann den eigenen Betrieb verlässt:
1. Der Betreiber schaltet sie in der .env frei (EXTERNAL_TRANSCRIPTION=mistral + MISTRAL_API_KEY). Standard: aus.
2. Die SL erlaubt sie pro Kampagne (Campaign.allowExternalTranscription, Standard: aus).
3. Der Auftrag wartet länger als EXTERNAL_AFTER_HOURS (Standard 24 h) …
4. … und in dieser ganzen Zeit war kein (nicht pausierter) Worker erreichbar.

Voxtral liefert Text, Zeiten und Sprecher-Kennungen, aber keine Stimmabdrücke → keine Wiedererkennung über
Stimmprofile und nichts wird gelernt; die SL ordnet von Hand zu (Vorstellungsrunde funktioniert weiter).
Aufnahmen über EXTERNAL_MAX_SECONDS werden geteilt; die Sprecher-Kennungen gelten dann nur je Teil.

API (geprüft am Mistral-SDK 2.10): POST /v1/audio/transcriptions, multipart; Listenfelder als wiederholte Felder;
Antwort: segments[{start, end, text, speaker_id}], usage.prompt_audio_seconds.
"""
from __future__ import annotations

import logging
import math
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app import audio, storage
from app.audio import AudioFehler
from app.config import get_settings
from app.db import utcnow
from app.models import Campaign, GameSession, Job, Upload, Worker

log = logging.getLogger("extern")
HALTER = "extern"  # lease_worker_id für Aufträge, die ein externer Anbieter bearbeitet
ANBIETER = {"mistral": "Mistral (Voxtral)"}


def konfig(db: Session):
    from app.einstellungen import extern_konfig

    return extern_konfig(db)


def anbieter(db: Session) -> str | None:
    """Freigegebener Anbieter (für /info) – nur, wenn auch ein Schlüssel hinterlegt ist."""
    return konfig(db).anbieter


# ---------------------------------------------------------------- Wann?
def ab_wann(db: Session, job: Job):
    return job.created_at + timedelta(hours=konfig(db).stunden)


def worker_seit(db: Session, zeitpunkt) -> bool:
    """War seit `zeitpunkt` irgendein (nicht pausierter, nicht gesperrter) Worker erreichbar?"""
    return db.scalar(select(func.count()).select_from(Worker).where(
        Worker.revoked_at.is_(None), Worker.paused.is_(False), Worker.last_seen_at >= zeitpunkt,
        or_(Worker.app_paused_since.is_(None), Worker.app_paused_since >= zeitpunkt),
        Worker.capabilities.contains("asr"))) > 0


def erlaubt(db: Session, job: Job) -> bool:
    if job.type != "transcribe" or not job.session_id:
        return False
    s = db.get(GameSession, job.session_id)
    c = db.get(Campaign, s.campaign_id) if s else None
    return bool(c and c.allow_external_transcription)


def faellig(db: Session, job: Job) -> bool:
    return (anbieter(db) is not None and job.state == "queued" and erlaubt(db, job)
            and utcnow() >= ab_wann(db, job) and not worker_seit(db, job.created_at))


# ---------------------------------------------------------------- Mistral
class MistralKlient:
    def __init__(self, k, client: httpx.Client | None = None):
        self.modell, self.url, self.key = k.modell, k.url, k.api_key
        self.client = client or httpx.Client(timeout=httpx.Timeout(900.0, connect=30.0))

    def transkribieren(self, datei: Path, sprecher_trennen: bool, hotwords: list[str]) -> dict:
        # Ohne `language`: Bei älteren Voxtral-Versionen ließen sich Sprache und Zeitstempel nicht kombinieren;
        # die Spracherkennung ist zuverlässig.
        felder: dict[str, str | list[str]] = {"model": self.modell,
                                              "diarize": "true" if sprecher_trennen else "false",
                                              "timestamp_granularities": ["segment"]}
        if hotwords:
            felder["context_bias"] = hotwords[:100]  # Listen werden als wiederholte Felder gesendet
        with open(datei, "rb") as f:
            r = self.client.post(self.url, headers={"Authorization": f"Bearer {self.key}"}, data=felder,
                                 files={"file": (datei.name, f, "audio/ogg")})
        if r.status_code in (401, 403):
            raise AudioFehler("external_auth", "Der externe Anbieter lehnt den API-Schlüssel ab.", retryable=False)
        if r.status_code == 429 or r.status_code >= 500:
            raise AudioFehler("external_unavailable", f"Der externe Anbieter ist gerade nicht erreichbar ({r.status_code}).",
                              retryable=True)
        if r.status_code >= 400:
            raise AudioFehler("external_rejected", f"Der externe Anbieter lehnt die Aufnahme ab ({r.status_code}): "
                                                   f"{r.text[:200]}", retryable=False)
        return r.json()


# ---------------------------------------------------------------- Verarbeitung
def _dateien_zusammensetzen(up: Upload, ziel: Path) -> list[tuple[dict, Path]]:
    """Teile je Datei zu einer Datei zusammensetzen (wie der Download des Workers)."""
    out = []
    for f in sorted(up.files, key=lambda x: x.position):
        pfad = ziel / f"{f.position:04d}-{f.id}"
        with open(pfad, "wb") as w:
            for i in range(f.chunk_count):
                w.write(storage.chunk_path(up.id, f.id, i).read_bytes())
        out.append(({"fileId": f.id, "position": f.position, "trackMemberId": f.track_member_id}, pfad))
    return out


def _teile(wav: Path, arbeit: Path, max_sek: float) -> list[tuple[Path, float]]:
    """WAV → Ogg/Opus (klein) in Stücken von höchstens max_sek. Gibt (Datei, Startversatz) zurück."""
    gesamt = audio.dauer(wav)
    teile, start, nr = [], 0.0, 0
    while start < gesamt - 0.5:
        laenge = min(max_sek, gesamt - start)
        ziel = arbeit / f"teil-{nr:02d}.ogg"
        res = audio.ffmpeg(["-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
                            "-t", f"{laenge:.2f}", *audio.EINGABE, "-i", str(wav), "-ac", "1", "-c:a", "libopus",
                            "-b:a", "32k",
                            str(ziel)], 900)
        if res.returncode != 0:
            raise AudioFehler("audio_unreadable", res.stderr.decode(errors="replace")[-300:])
        teile.append((ziel, start))
        start, nr = start + laenge, nr + 1
    return teile


def _segmente(antwort: dict, versatz: float, label) -> list[dict]:
    out = []
    for seg in antwort.get("segments") or []:
        if seg.get("start") is None or seg.get("end") is None:
            continue
        roh = seg.get("speaker_id") if seg.get("speaker_id") is not None else seg.get("speaker")
        out.append({"start": float(seg["start"]) + versatz, "end": float(seg["end"]) + versatz,
                    "text": seg.get("text") or "", "speaker": label(roh)})
    return out


def verarbeiten(db: Session, job: Job, klient: MistralKlient | None = None) -> None:
    """Einen reservierten Auftrag extern transkribieren und wie ein Worker-Ergebnis übernehmen."""
    from app import queue
    from app.namenshilfe import hotwords_fuer_session
    from app.routers.worker import ResultIn, ergebnis_uebernehmen
    from app.transkription import _bereinigen, halluzinationen_entfernen, sprecher_auswerten

    k = konfig(db)
    klient = klient or MistralKlient(k)
    sitzung = db.get(GameSession, job.session_id)
    up = db.get(Upload, job.upload_id)
    hotwords = hotwords_fuer_session(db, db.get(Campaign, sitzung.campaign_id), sitzung)
    arbeit = Path(tempfile.mkdtemp(prefix="extern-", dir=str(storage.uploads_root().parent)))
    t0 = time.monotonic()
    try:
        dateien = _dateien_zusammensetzen(up, arbeit)
        segmente, stimmen, gesamt, abgerechnet = [], [], 0.0, 0.0
        if up.source == "discord":
            for nr, (f, pfad) in enumerate(dateien):
                wav = arbeit / f"spur-{nr}.wav"
                audio.zusammenfuegen([pfad], wav)
                gesamt = max(gesamt, audio.dauer(wav))
                spur = []
                for teil, versatz in _teile(wav, arbeit, k.max_sekunden):
                    antwort = klient.transkribieren(teil, False, hotwords)
                    abgerechnet += float((antwort.get("usage") or {}).get("prompt_audio_seconds") or 0)
                    spur += _segmente(antwort, versatz, lambda _r, fid=f["fileId"]: fid)
                spur = halluzinationen_entfernen(_bereinigen(spur, hotwords), nur_ohne_sprecher=False)
                a, b = sprecher_auswerten(spur, wav, arbeit, track_member_id=f["trackMemberId"])
                segmente += a
                stimmen += b
        else:
            wav = arbeit / "gesamt.wav"
            audio.zusammenfuegen([p for _, p in dateien], wav)
            gesamt = audio.dauer(wav)
            roh = []
            for nr, (teil, versatz) in enumerate(_teile(wav, arbeit, k.max_sekunden)):
                antwort = klient.transkribieren(teil, True, hotwords)
                abgerechnet += float((antwort.get("usage") or {}).get("prompt_audio_seconds") or 0)
                # Sprecher-Kennungen gelten nur innerhalb eines Teils
                roh += _segmente(antwort, versatz, lambda r, n=nr: None if r is None else f"T{n}-{r}")
            roh = halluzinationen_entfernen(_bereinigen(roh, hotwords))
            segmente, stimmen = sprecher_auswerten(roh, wav, arbeit)
        minuten = (abgerechnet or gesamt) / 60
        body = ResultIn.model_validate({"audioSeconds": round(gesamt, 1),
                                        "computeSeconds": round(time.monotonic() - t0, 1),
                                        "model": f"mistral/{klient.modell}", "segments": segmente,
                                        "speakers": stimmen})
        db.refresh(job)
        if job.state != "leased" or job.lease_worker_id != HALTER:
            return  # inzwischen anders erledigt
        ergebnis_uebernehmen(db, job, body, engine="external", worker_id=None,
                             kosten_cent=math.ceil(minuten * k.cent_pro_minute))
        db.commit()
    except AudioFehler as e:
        db.rollback()
        queue.fail_job(db, db.get(Job, job.id), e.code, str(e), e.retryable)
        db.commit()
    except Exception as e:  # Netz weg, unerwartete Antwort …
        db.rollback()
        log.exception("Externe Transkription fehlgeschlagen")
        queue.fail_job(db, db.get(Job, job.id), "external_error", f"{type(e).__name__}: {e}"[:300], True)
        db.commit()
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)


def einen_auftrag(db: Session, klient: MistralKlient | None = None) -> bool:
    """Ältesten fälligen Auftrag reservieren und extern bearbeiten. True, wenn einer bearbeitet wurde."""
    from app import kosten

    if anbieter(db) is None or kosten.erreicht(db):
        return False
    for job in db.scalars(select(Job).where(Job.state == "queued", Job.type == "transcribe").order_by(Job.created_at)):
        if not faellig(db, job):
            continue
        now = utcnow()
        res = db.execute(update(Job).where(Job.id == job.id, Job.state == "queued").values(
            state="leased", lease_worker_id=HALTER, lease_expires_at=now + timedelta(hours=3),
            attempts=Job.attempts + 1, started_at=now, engine="external"))
        if res.rowcount != 1:
            db.rollback()
            continue
        from app.services import set_state

        set_state(db.get(GameSession, job.session_id), "transcribing", progress=None)
        db.commit()
        db.refresh(job)
        log.info("Session %s wird extern transkribiert (%s)", job.session_id, anbieter(db))
        verarbeiten(db, job, klient)
        return True
    return False


def arbeitsprozess_starten(session_factory) -> threading.Event | None:
    intervall = get_settings().external_interval_seconds
    if intervall <= 0:
        return None  # prüft bei jedem Durchlauf, ob die Verwaltung die externe Transkription freigegeben hat
    stop = threading.Event()

    def schleife():
        while not stop.wait(intervall):
            try:
                with session_factory() as db:
                    while einen_auftrag(db) and not stop.is_set():
                        pass
            except Exception:
                log.exception("Fehler im Prozess für externe Transkription")

    threading.Thread(target=schleife, name="extern", daemon=True).start()
    return stop
