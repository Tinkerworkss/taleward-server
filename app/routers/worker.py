"""Worker-Protokoll (intern, nicht Teil der App-YAML): Worker holen Aufträge ab und liefern Ergebnisse.

Anmeldung mit Worker-Token (nicht dem Nutzer-JWT): Authorization: Bearer wk.<workerId>.<geheimnis>
Download der Audio-Teile nur für den Auftrag, dessen Lease der Worker gerade hält.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import errors, storage
from app.config import get_settings
from app.db import get_db, utcnow
from app.models import (
    Attendee, Campaign, GameSession, Job, Speaker, TranscriptSegment, Upload, UsageLog, Worker,
)
from app.queue import claim, create_summarize_job, extend_lease, fail_job, holds_lease, stimmen_vergessen
from app.schemas import ApiModel
from app.services import set_state

router = APIRouter(prefix="/worker/v1", include_in_schema=False)

MAX_SAMPLE_BYTES = 512 * 1024


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def current_worker(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Worker:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise errors.UNAUTHENTICATED
    teile = authorization[7:].strip().split(".")
    if len(teile) != 3 or teile[0] != "wk":
        raise errors.TOKEN_INVALID
    w = db.get(Worker, teile[1])
    if w is None or w.revoked_at is not None or not hmac.compare_digest(w.token_hash, token_hash(teile[2])):
        raise errors.TOKEN_INVALID
    w.last_seen_at = utcnow()
    db.commit()
    return w


# ---------- Modelle ----------
class ClaimIn(ApiModel):
    capabilities: list[str] = ["asr"]
    info: dict = {}
    wait_seconds: int | None = Field(default=None, ge=0, le=60)


class ProgressIn(ApiModel):
    progress: float | None = Field(default=None, ge=0, le=1)
    # 0.4.6: Zwischenschritt der Zusammenfassung → ProcessingStatus.message „summarizing.<step>“
    step: Literal["notes", "recap", "review", "revision", "proposals"] | None = None


class LowWordIn(ApiModel):
    word: str = Field(max_length=100)
    start: float = Field(ge=0)
    score: float = Field(ge=0, le=1)
    anfang: bool = False  # erstes Wort eines Satzes (Großschreibung sagt dann nichts)


class SegmentIn(ApiModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str | None = None
    text: str = Field(max_length=20000)
    low_words: list[LowWordIn] = Field(default=[], max_length=500)  # 0.4.6: unsicher ausgerichtete Wörter


class SpeakerIn(ApiModel):
    label: str = Field(max_length=50)  # Kennung des Workers, z. B. SPEAKER_01 oder die fileId bei Discord
    speaking_seconds: float = Field(ge=0)
    sample_text: str = Field(default="", max_length=2000)
    sample_ogg_base64: str | None = None
    embedding: list[float] | None = Field(default=None, max_length=4096)
    track_member_id: str | None = None


class ResultIn(ApiModel):
    audio_seconds: float = Field(ge=0)
    compute_seconds: float = Field(ge=0)
    model: str | None = Field(default=None, max_length=100)
    peak_vram_mb: int | None = None
    segments: list[SegmentIn] = Field(max_length=200000)
    speakers: list[SpeakerIn] = Field(max_length=50)
    embedding_model: str | None = Field(default=None, max_length=100)  # Sprechermodell samt Fassung


class FailIn(ApiModel):
    code: str = Field(max_length=64)
    message: str = Field(max_length=2000)
    retryable: bool = True


# ---------- Hilfen ----------
def _job_for(db: Session, job_id: str, worker: Worker) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise errors.not_found()
    if not holds_lease(job, worker):
        raise errors.conflict("lease_lost")
    return job


def _stimm_auftrag(job: Job) -> dict:
    """Stimmprofil: nur die Aufnahme, ohne Namen oder andere Angaben zur Person."""
    from app.stimmprofile import audio_pfad

    groesse = audio_pfad(job.id).stat().st_size
    return {
        "jobId": job.id, "type": job.type, "leaseSeconds": get_settings().lease_seconds, "attempt": job.attempts,
        "files": [{"fileId": "stimme", "position": 0, "fileName": "stimme", "mimeType": "audio/*",
                   "sizeBytes": groesse, "trackMemberId": None,
                   "chunks": [{"index": 0, "sizeBytes": groesse, "url": f"/worker/v1/jobs/{job.id}/voice-audio"}]}],
    }


def _modell_angabe(k) -> dict:
    """„auto“: Der Worker wählt Modell und Kontext nach seiner Grafikkarte (app/recapmodell.py). Ein Modellname
    steht trotzdem dabei – für Worker, die „auto“ noch nicht kennen."""
    from app import recapmodell

    if k.lokal_modell == recapmodell.AUTO:
        return {"model": recapmodell.KLEIN, "context": k.lokal_kontext, "auto": True}
    return {"model": k.lokal_modell, "context": k.lokal_kontext}


def _zusammenfassungs_auftrag(db: Session, job: Job) -> dict:
    """Zusammenfassung auf einem Worker mit Sprachmodell: die beiden getrennten Eingaben, kein Audio."""
    from app.einstellungen import llm_konfig
    from app.zusammenfassung import eingabe_bauen, gegenpruefen_an, recap_eingabe, vorschlag_eingabe

    s = db.get(GameSession, job.session_id)
    k = llm_konfig(db)
    basis = eingabe_bauen(db, s)
    return {
        "jobId": job.id, "type": job.type, "leaseSeconds": get_settings().lease_seconds, "attempt": job.attempts,
        "files": [],
        "summarize": {"recap": recap_eingabe(basis), "proposals": vorschlag_eingabe(db, s, basis),
                      **_modell_angabe(k), "review": gegenpruefen_an(db)},
    }


def _auftrag(db: Session, job: Job) -> dict:
    if job.type == "voice_enroll":
        return _stimm_auftrag(job)
    if job.type == "summarize":
        return _zusammenfassungs_auftrag(db, job)
    if job.type == "document":
        from app.einstellungen import llm_konfig
        from app.models import CampaignDocument
        from app.unterlagen import eingabe

        k = llm_konfig(db)
        return {"jobId": job.id, "type": job.type, "leaseSeconds": get_settings().lease_seconds,
                "attempt": job.attempts, "files": [],
                "document": {"input": eingabe(db, db.get(CampaignDocument, job.document_id)),
                             **_modell_angabe(k)}}
    s = db.get(GameSession, job.session_id)
    c = db.get(Campaign, s.campaign_id)
    up = db.get(Upload, job.upload_id)
    anwesend = db.scalars(select(Attendee).where(Attendee.session_id == s.id)).all()
    files = []
    for f in up.files:
        chunks = []
        for i in range(f.chunk_count):
            groesse = up.chunk_size if i < f.chunk_count - 1 else f.size_bytes - up.chunk_size * (f.chunk_count - 1)
            chunks.append({"index": i, "sizeBytes": groesse,
                           "url": f"/worker/v1/jobs/{job.id}/files/{f.id}/chunks/{i}"})
        files.append({"fileId": f.id, "position": f.position, "fileName": f.file_name, "mimeType": f.mime_type,
                      "sizeBytes": f.size_bytes, "trackMemberId": f.track_member_id, "chunks": chunks})
    return {
        "jobId": job.id, "type": job.type, "leaseSeconds": get_settings().lease_seconds,
        "attempt": job.attempts,
        "session": {
            "language": c.language, "system": c.system, "systemName": c.system_name, "source": up.source,
            "expectedSpeakers": len(anwesend) if up.source == "table" else len(up.files),
            # Vollständigkeit vor Terminologie: Whisper bekommt produktiv keine Prompt-Hotwords.
            # Korrekturen laufen nach der ASR zentral und verändern keine Segmentgrenzen.
            "hotwords": [],
            # 0.4.6: erneute Transkription – nur der Text, Stimmen sind schon zugeordnet
            "nurText": bool(s.nachtranskription),
        },
        "files": files,
    }


# ---------- Endpunkte ----------
@router.post("/jobs/claim")
def claim_job(body: ClaimIn, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    erlaubt = set(worker.capabilities.split(","))
    # In der Worker-App pausiert: Der Worker meldet sich nur (Lebenszeichen), holt nichts ab
    app_pause = bool(body.info.pop("pausiert", False))
    if app_pause:
        worker.app_paused_since = worker.app_paused_since or utcnow()
    else:
        worker.app_paused_since = None
    caps = [c for c in body.capabilities if c in erlaubt] if not (worker.paused or app_pause) else []
    if "llm" in caps:
        from app.einstellungen import llm_konfig

        if llm_konfig(db).art != "lokal":
            caps.remove("llm")  # Zusammenfassung macht gerade die Zentrale (API/Attrappe) oder niemand
    worker.info = json.dumps(body.info, ensure_ascii=False)[:2000]
    db.commit()
    warte = get_settings().claim_wait_seconds if body.wait_seconds is None else body.wait_seconds
    if app_pause:
        return _ohne_auftrag(worker.paused)
    ende = time.monotonic() + warte
    while True:
        if caps and db.get(Worker, worker.id).paused:  # während des Wartens pausiert
            caps = []
        job = claim(db, worker, caps) if caps else None
        if job is not None:
            return _auftrag(db, job)
        if time.monotonic() >= ende:
            return _ohne_auftrag(db.get(Worker, worker.id).paused)
        time.sleep(1)
        db.expire_all()
        worker.last_seen_at = utcnow()
        db.commit()


def _ohne_auftrag(in_verwaltung_pausiert: bool) -> Response:
    """204 ohne Auftrag. Ist der Worker in der Verwaltung pausiert, steht das im Kopf – die Worker-App zeigt es an."""
    return Response(status_code=204, headers={"X-Taleward-Pausiert": "verwaltung"} if in_verwaltung_pausiert else {})


@router.get("/jobs/{jobId}/files/{fileId}/chunks/{index}")
def download_chunk(jobId: str, fileId: str, index: int, worker: Worker = Depends(current_worker),
                   db: Session = Depends(get_db)):
    job = _job_for(db, jobId, worker)
    pfad = storage.chunk_path(job.upload_id, fileId, index)
    if not pfad.exists():
        raise errors.not_found()
    return FileResponse(pfad, media_type="application/octet-stream")


@router.get("/jobs/{jobId}/voice-audio")
def download_voice(jobId: str, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    from app.stimmprofile import audio_pfad

    job = _job_for(db, jobId, worker)
    pfad = audio_pfad(job.id)
    if job.type != "voice_enroll" or not pfad.exists():
        raise errors.not_found()
    return FileResponse(pfad, media_type="application/octet-stream")


class VoiceResultIn(ApiModel):
    embedding: list[float] = Field(min_length=16, max_length=4096)
    speech_seconds: float = Field(ge=0)
    audio_seconds: float = Field(ge=0)
    compute_seconds: float = Field(default=0, ge=0)
    model: str | None = Field(default=None, max_length=100)


@router.post("/jobs/{jobId}/voice-result", status_code=204)
def voice_result(jobId: str, body: VoiceResultIn, worker: Worker = Depends(current_worker),
                 db: Session = Depends(get_db)):
    from app.stimmprofile import ergebnis

    job = _job_for(db, jobId, worker)
    if job.type != "voice_enroll":
        raise errors.not_found()
    ergebnis(db, job, body.embedding, body.speech_seconds, body.model)
    job.state, job.finished_at, job.progress, job.lease_expires_at = "done", utcnow(), 1.0, None
    db.commit()
    return Response(status_code=204)


@router.post("/jobs/{jobId}/progress", status_code=204)
def progress(jobId: str, body: ProgressIn, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    job = _job_for(db, jobId, worker)
    extend_lease(db, job, body.progress, body.step)
    db.commit()
    return Response(status_code=204)


@router.post("/jobs/{jobId}/fail", status_code=204)
def fail(jobId: str, body: FailIn, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    job = _job_for(db, jobId, worker)
    fail_job(db, job, body.code, body.message, body.retryable)
    db.commit()
    return Response(status_code=204)


@router.post("/jobs/{jobId}/result", status_code=204)
def result(jobId: str, body: ResultIn, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    job = _job_for(db, jobId, worker)
    if job.type != "transcribe":
        raise errors.not_found()
    ergebnis_uebernehmen(db, job, body, engine="local", worker_id=worker.id)
    from app import eingebaut

    eingebaut.messung_merken(db, worker.id, body.audio_seconds, body.compute_seconds, body.model, body.peak_vram_mb)
    db.commit()
    return Response(status_code=204)


def ergebnis_uebernehmen(db: Session, job: Job, body: ResultIn, engine: str, worker_id: str | None,
                         kosten_cent: int = 0) -> None:
    """Transkript übernehmen – gleicher Weg für eigene Worker (local) und externe Anbieter (external):
    Stimmen, Hörproben, Transkript, Verbrauch, Audio löschen (oder bis zur Freigabe behalten), Vorschläge bzw. weiter zur Zusammenfassung."""
    s = db.get(GameSession, job.session_id)
    up = db.get(Upload, job.upload_id)
    discord = up.source == "discord"
    if s.nachtranskription:
        return _nachtranskription_uebernehmen(db, s, job, body, engine, worker_id, kosten_cent, discord)
    # Ergebnis eines früheren Versuchs verwerfen
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.session_id == s.id))
    db.execute(delete(Speaker).where(Speaker.session_id == s.id))
    storage.delete_samples(s.id)

    sprecher = sorted(body.speakers, key=lambda x: -x.speaking_seconds)
    zuordnung: dict[str, str] = {}
    for pos, sp in enumerate(sprecher):
        obj = Speaker(session_id=s.id, position=pos, label=f"Stimme {pos + 1}", raw_label=sp.label,
                      speaking_seconds=round(sp.speaking_seconds, 1), sample_text=sp.sample_text.strip(),
                      embedding=json.dumps(sp.embedding) if sp.embedding else None,
                      embedding_model=body.embedding_model if sp.embedding else None)
        if discord and sp.track_member_id:
            obj.source, obj.confidence = "discord_track", 1.0
            obj.suggested_member_id = obj.assigned_member_id = sp.track_member_id
        db.add(obj)
        db.flush()
        zuordnung[sp.label] = obj.id
        if sp.sample_ogg_base64:
            daten = base64.b64decode(sp.sample_ogg_base64)
            if len(daten) <= MAX_SAMPLE_BYTES:
                storage.write_atomic(storage.sample_path(s.id, obj.id), daten)
                obj.sample_path = str(storage.sample_path(s.id, obj.id))
    for pos, seg in enumerate(sorted(body.segments, key=lambda x: x.start)):
        db.add(TranscriptSegment(session_id=s.id, position=pos, start=round(seg.start, 2), end=round(seg.end, 2),
                                 speaker_id=zuordnung.get(seg.speaker or ""), text=seg.text.strip(),
                                 unsicher=_unsicher_json(seg)))
    db.add(UsageLog(campaign_id=s.campaign_id, session_id=s.id, kind="transcription", engine=engine,
                    model=body.model, worker_id=worker_id, audio_seconds=body.audio_seconds,
                    compute_seconds=body.compute_seconds, cost_cents=kosten_cent))
    # Audio: je nach Einstellung sofort löschen oder bis zur Freigabe behalten (app/aufbewahrung.py)
    from app import aufbewahrung

    if not aufbewahrung.lesen(db).bis_freigabe:
        storage.delete_upload_files(up.id)
        s.audio_deleted_at = utcnow()
    now = utcnow()
    s.duration_seconds = int(round(body.audio_seconds))
    s.transcription_engine = engine
    job.state, job.finished_at, job.progress, job.engine = "done", now, 1.0, engine
    job.lease_expires_at = None  # lease_worker_id bleibt: wer es gemacht hat (Verwaltung)
    if discord:
        # Stimmen sind über die Spuren schon zugeordnet: Hörproben und Abdrücke sofort löschen, weiter zur Zusammenfassung
        db.flush()
        stimmen_vergessen(db, s)
        create_summarize_job(db, s)
    else:
        db.flush()
        try:  # Vorschläge aus Vorstellungsrunde und Stimmprofilen – Fehler hier dürfen nichts blockieren
            from app.zuordnung import vorschlagen

            vorschlagen(db, s)
        except Exception:
            import logging

            logging.getLogger("zuordnung").exception("Stimmvorschläge für Session %s fehlgeschlagen", s.id)
        set_state(s, "awaiting_speakers")


def _unsicher_json(seg: SegmentIn) -> str | None:
    if not seg.low_words:
        return None
    return json.dumps([{"word": w.word, "start": round(w.start, 2), "score": round(w.score, 3), "anfang": w.anfang}
                       for w in seg.low_words], ensure_ascii=False)


def _nachtranskription_uebernehmen(db: Session, s: GameSession, job: Job, body: ResultIn, engine: str,
                                   worker_id: str | None, kosten_cent: int, discord: bool) -> None:
    """Erneute Transkription (0.4.6, POST …/corrections mit retranscribe): Nur der Text wird ersetzt. Die bestätigte
    Stimmzuordnung bleibt – jeder neue Abschnitt bekommt die Stimme des alten Abschnitts, mit dem er sich zeitlich am
    meisten überschneidet (Discord: die Spur). Neue Stimmen und Hörproben des Workers werden verworfen."""
    alt = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)
                          .order_by(TranscriptSegment.start)))
    spur = {sp.raw_label: sp.id for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))} if discord else {}

    def stimme(start: float, ende: float, label: str | None) -> str | None:
        if discord and label and label in spur:
            return spur[label]
        bester, wert = None, 0.0
        for a in alt:
            if a.end < start - 2 or a.start > ende + 2:
                continue
            ueberlappung = min(a.end, ende) - max(a.start, start)
            abstand = -min(abs(a.start - start), abs(a.end - ende))
            w = ueberlappung if ueberlappung > 0 else abstand / 100
            if bester is None or w > wert:
                bester, wert = a.speaker_id, w
        return bester

    neu = [(seg, stimme(seg.start, seg.end, seg.speaker)) for seg in sorted(body.segments, key=lambda x: x.start)]
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.session_id == s.id))
    for pos, (seg, sid) in enumerate(neu):
        db.add(TranscriptSegment(session_id=s.id, position=pos, start=round(seg.start, 2), end=round(seg.end, 2),
                                 speaker_id=sid, text=seg.text.strip(), unsicher=_unsicher_json(seg)))
    db.add(UsageLog(campaign_id=s.campaign_id, session_id=s.id, kind="transcription", engine=engine,
                    model=body.model, worker_id=worker_id, audio_seconds=body.audio_seconds,
                    compute_seconds=body.compute_seconds, cost_cents=kosten_cent))
    from app import aufbewahrung

    if not aufbewahrung.lesen(db).bis_freigabe:
        storage.delete_upload_files(db.get(Upload, job.upload_id).id)
        s.audio_deleted_at = utcnow()
    s.nachtranskription = False
    s.transcription_engine = engine
    job.state, job.finished_at, job.progress, job.engine = "done", utcnow(), 1.0, engine
    job.lease_expires_at = None
    db.flush()
    create_summarize_job(db, s)


class ProposalIn(ApiModel):
    entry_type: str
    action: str
    target_entry_id: str | None = None
    title: str = Field(max_length=300)
    detail: str = Field(default="", max_length=4000)
    gm_notes: str | None = Field(default=None, max_length=4000)
    suggested_visibility: str = "gm_only"
    visibility_reason: str | None = Field(default=None, max_length=500)
    confidence: float = Field(default=0.5, ge=0, le=1)
    flags: list[str] = Field(default=[], max_length=5)
    evidence: list[dict] = Field(default=[], max_length=5)


class SummaryResultIn(ApiModel):
    title: str = Field(default="", max_length=300)
    text: str = Field(min_length=1, max_length=40000)
    open_threads: list[str] = Field(default=[], max_length=20)
    proposals: list[ProposalIn] = Field(default=[], max_length=30)
    review: dict | None = None  # 0.4.6: Gegenprüfung (sprachmodell.Ablauf.gegenpruefen)
    model: str | None = Field(default=None, max_length=100)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    compute_seconds: float = Field(default=0, ge=0)


@router.post("/jobs/{jobId}/summary-result", status_code=204)
def summary_result(jobId: str, body: SummaryResultIn, worker: Worker = Depends(current_worker),
                   db: Session = Depends(get_db)):
    """Ergebnis eines Workers mit Sprachmodell. Wird noch einmal geprüft wie eine Antwort der API."""
    from app.sprachmodell import pruefen
    from app.zusammenfassung import ergebnis_aus, speichern, vorschlag_eingabe, eingabe_bauen

    job = _job_for(db, jobId, worker)
    if job.type != "summarize":
        raise errors.not_found()
    s = db.get(GameSession, job.session_id)
    ein = vorschlag_eingabe(db, s, eingabe_bauen(db, s))
    roh = [p.model_dump(by_alias=True) for p in body.proposals]
    d = {"title": body.title, "text": body.text, "openThreads": body.open_threads, "model": body.model,
         "tokensIn": body.tokens_in, "tokensOut": body.tokens_out, "review": body.review,
         "proposals": pruefen(roh, {e["id"] for e in ein["bibel"]}, {e["id"] for e in ein["geheim"]},
                              charaktere=[p["charakter"] for p in ein["personen"] if p.get("charakter")],
                              namen={e["id"]: e["name"] for e in ein["bibel"] + ein["geheim"]})}
    speichern(db, s, ergebnis_aus(d), body.compute_seconds, engine="local", worker_id=worker.id)
    job.state, job.finished_at, job.progress, job.lease_expires_at = "done", utcnow(), 1.0, None
    db.commit()
    return Response(status_code=204)


class DocumentResultIn(ApiModel):
    proposals: list[dict] = Field(default=[], max_length=100)
    world_info_suggestion: str | None = Field(default=None, max_length=5000)
    model: str | None = Field(default=None, max_length=100)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    compute_seconds: float = Field(default=0, ge=0)


@router.post("/jobs/{jobId}/document-result", status_code=204)
def document_result(jobId: str, body: DocumentResultIn, worker: Worker = Depends(current_worker),
                    db: Session = Depends(get_db)):
    """Ergebnis eines Workers mit Sprachmodell für eine SL-Unterlage. Die Regeln zur Trennung von SL- und
    Spielerwissen setzt der Server beim Speichern selbst durch."""
    from app.models import CampaignDocument
    from app.unterlagen import speichern

    job = _job_for(db, jobId, worker)
    doc = db.get(CampaignDocument, job.document_id or "")
    if job.type != "document" or doc is None:
        raise errors.not_found()
    speichern(db, doc, {"proposals": body.proposals, "worldInfoSuggestion": body.world_info_suggestion,
                        "model": body.model, "tokensIn": body.tokens_in, "tokensOut": body.tokens_out},
              "local", worker.id, body.compute_seconds)
    job.state, job.finished_at, job.progress, job.lease_expires_at = "done", utcnow(), 1.0, None
    db.commit()
    return Response(status_code=204)


# ---------- Koppeln und gemeinsame Einstellungen ----------
class PairIn(ApiModel):
    code: str = Field(max_length=20)
    name: str = Field(default="worker", max_length=100)


@router.post("/pair", status_code=201)
def pair(body: PairIn, request: Request, db: Session = Depends(get_db)):
    """Kurzen Code aus der Verwaltung gegen einen Zugangsschlüssel tauschen (ohne Anmeldung, Versuche begrenzt)."""
    from app.koppeln import koppeln

    w, token = koppeln(db, request.client.host if request.client else "?", body.code, body.name)
    return {"workerId": w.id, "name": w.name, "token": token, "serverVersion": server_fassung()}


@router.get("/config")
def config(worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    """Einstellungen, die der Server für alle Worker verwaltet. Liegt das Sprechermodell auf dem Server, holen die
    Worker es von dort (models) und bekommen keinen Hugging-Face-Zugang mehr – er bleibt auf dem Server."""
    from app import modellablage
    from app.einstellungen import meta_lesen

    stand = modellablage.vorhanden(db)
    if stand is not None:
        antwort = {"hfToken": None, "models": {stand.repo: stand.fassung}, "serverVersion": server_fassung()}
    else:
        antwort = {"hfToken": meta_lesen(db, "hf.token") or None, "models": {}, "serverVersion": server_fassung()}
    if worker.local:  # eingebauter Worker: Einstellungen aus der Verwaltung
        from app import eingebaut

        antwort["eingebaut"] = eingebaut.fuer_worker(db)
    return antwort


@router.get("/app-update")
def app_update(request: Request, system: str = "windows", worker: Worker = Depends(current_worker),
               db: Session = Depends(get_db)):
    """Neueste freigegebene Fassung der Worker-App für dieses System – die Datei liegt auf diesem Server
    (/downloads/…), der Worker fragt nie direkt bei GitHub. 204 = nichts bekannt."""
    from app import aktualisierung
    from app.einstellungen import oeffentliche_adresse

    art = "worker-linux" if system == "linux" else "worker-windows"
    angebot = aktualisierung.angebot(db, art, oeffentliche_adresse(db, request))
    if angebot is None:
        return Response(status_code=204)
    # Die Freigabe (Text und Unterschrift) reicht der Server nur durch – der Worker prüft sie selbst
    s = aktualisierung.freigegeben(db, art) or {}
    zusatz = {"freigabe": s.get("freigabe"), "freigabeSignatur": s.get("freigabe_sig"), "commit": s.get("commit")}
    return {**angebot, "repo": aktualisierung.ARTEN[art].repo(), **{k: v for k, v in zusatz.items() if v}}


def server_fassung() -> str:
    """Fassung des Servers – die Worker-App installiert dazu passend dieselbe Fassung als KI-Paket."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("taleward-server")
    except PackageNotFoundError:
        return "0.0.0"


@router.get("/models")
def modell_verzeichnis(repo: str, worker: Worker = Depends(current_worker), db: Session = Depends(get_db)):
    """Verzeichnis der festen Fassung eines Modells, das der Server verteilt (Pfade, Größen, SHA-256)."""
    from app import modellablage

    stand = modellablage.vorhanden(db, repo)
    if stand is None:
        raise errors.ApiError(404, "model_not_on_server")
    return {"repo": stand.repo, "fassung": stand.fassung, "dateien": stand.dateien}


@router.get("/models/file")
def modell_datei(repo: str, fassung: str, pfad: str, worker: Worker = Depends(current_worker),
                 db: Session = Depends(get_db)):
    from app import modellablage

    datei = modellablage.datei(db, repo, fassung, pfad)
    if datei is None:
        raise errors.ApiError(404, "model_not_on_server")
    return FileResponse(datei, media_type="application/octet-stream")
