"""Warteschlange: Aufträge anlegen, vergeben (Lease), abschließen, zurückholen. Alles in SQLite.

Zustände eines Auftrags: queued → leased → done | failed (bei Lease-Ablauf zurück auf queued).
"""
import json
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import GameSession, Job, Upload, Worker
from app.services import set_state
from app import storage


def status_message(key: str, **params) -> str:
    """Status-Hinweis als Schlüssel speichern – übersetzt wird erst beim Ausliefern (Accept-Language)."""
    return json.dumps({"key": key, **params}, ensure_ascii=False)


def create_transcribe_job(db: Session, s: GameSession, upload: Upload) -> Job:
    job = Job(type="transcribe", session_id=s.id, upload_id=upload.id, required_capability="asr", engine="local")
    db.add(job)
    set_state(s, "queued", progress=0.0)
    return job


def create_summarize_job(db: Session, s: GameSession) -> Job:
    """Zusammenfassung einreihen (Fähigkeit llm: die Zentrale per API/Attrappe oder ein Worker mit Ollama)."""
    job = Job(type="summarize", session_id=s.id, required_capability="llm", engine="local")
    db.add(job)
    set_state(s, "summarizing", progress=None)
    return job


def stimmen_vergessen(db: Session, s: GameSession) -> None:
    """Nach bestätigter Zuordnung: Hörproben und Stimmabdrücke der Session löschen (Pflichtregel)."""
    from app.models import Speaker

    storage.delete_samples(s.id)
    for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id)):
        sp.sample_path = None
        sp.embedding = None


def current_job(db: Session, session_id: str, type_: str | None = "transcribe") -> Job | None:
    """Neuester Auftrag einer Session (type_=None: egal welcher Art)."""
    q = select(Job).where(Job.session_id == session_id)
    if type_ is not None:
        q = q.where(Job.type == type_)
    return db.scalar(q.order_by(Job.created_at.desc()).limit(1))


def queue_position(db: Session, job: Job) -> int | None:
    if job.state != "queued":
        return None
    vor = db.scalar(
        select(func.count()).select_from(Job).where(
            Job.state == "queued", Job.required_capability == job.required_capability, Job.created_at < job.created_at
        )
    )
    return vor + 1


def llm_worker_online(db: Session) -> bool:
    """Ist ein Worker mit Sprachmodell (Ollama) verbunden und nicht pausiert?"""
    import json

    grenze = utcnow() - timedelta(seconds=get_settings().worker_offline_after_seconds)
    for w in db.scalars(select(Worker).where(Worker.revoked_at.is_(None), Worker.last_seen_at > grenze,
                                             Worker.paused.is_(False))):
        try:
            if "llm" in w.capabilities.split(",") and json.loads(w.info or "{}").get("llm"):
                return True
        except ValueError:
            continue
    return False


def worker_online(db: Session, capability: str = "asr") -> bool:
    grenze = utcnow() - timedelta(seconds=get_settings().worker_offline_after_seconds)
    for w in db.scalars(select(Worker).where(Worker.revoked_at.is_(None), Worker.last_seen_at > grenze)):
        if capability in w.capabilities.split(","):
            return True
    return False


def kampagne_von(db: Session, job: Job):
    from app.models import Campaign, CampaignDocument

    if job.session_id:
        s = db.get(GameSession, job.session_id)
        return db.get(Campaign, s.campaign_id) if s else None
    if job.document_id:
        d = db.get(CampaignDocument, job.document_id)
        return db.get(Campaign, d.campaign_id) if d else None
    return None


def cloud_erlaubt(db: Session, job: Job) -> bool:
    """0.3.10: Recap/Vorschläge bzw. Unterlage dieser Kampagne dürfen an die Cloud-API."""
    c = kampagne_von(db, job)
    return bool(c and c.allow_cloud_summary)


def claim(db: Session, worker: Worker, capabilities: list[str], darf=None) -> Job | None:
    """Ältesten passenden Auftrag vergeben. Atomar: gelingt nur einem Worker.
    `darf(db, job)` kann Aufträge überspringen (z. B. Kampagnen ohne Erlaubnis für die Cloud)."""
    s = get_settings()
    kandidaten = db.scalars(
        select(Job.id).where(Job.state == "queued", Job.required_capability.in_(capabilities))
        .order_by(Job.created_at).limit(5 if darf is None else 200)
    ).all()
    if darf is not None:
        kandidaten = [i for i in kandidaten if darf(db, db.get(Job, i))][:5]
    now = utcnow()
    for job_id in kandidaten:
        res = db.execute(
            update(Job).where(Job.id == job_id, Job.state == "queued").values(
                state="leased", lease_worker_id=worker.id,
                lease_expires_at=now + timedelta(seconds=s.lease_seconds),
                attempts=Job.attempts + 1, started_at=now, progress=0.0,
            )
        )
        if res.rowcount == 1:
            db.commit()
            job = db.get(Job, job_id)
            db.refresh(job)
            if job.type == "document" and job.document_id:
                from app.models import CampaignDocument

                doc = db.get(CampaignDocument, job.document_id)
                if doc is not None:
                    doc.state, doc.progress = "processing", 0.0
                    db.commit()
            if job.session_id:
                sess = db.get(GameSession, job.session_id)
                if job.type == "transcribe":
                    set_state(sess, "transcribing", progress=0.0)
                    db.commit()
            return job
    return None


def holds_lease(job: Job | None, worker: Worker) -> bool:
    return (
        job is not None and job.state == "leased" and job.lease_worker_id == worker.id
        and job.lease_expires_at is not None and job.lease_expires_at > utcnow()
    )


def extend_lease(db: Session, job: Job, progress: float | None) -> None:
    job.lease_expires_at = utcnow() + timedelta(seconds=get_settings().lease_seconds)
    if progress is not None:
        job.progress = max(0.0, min(1.0, progress))
        if job.type == "document" and job.document_id:
            from app.models import CampaignDocument

            doc = db.get(CampaignDocument, job.document_id)
            if doc is not None and doc.state == "processing":
                doc.progress = job.progress
        if job.session_id and job.type == "transcribe":
            sess = db.get(GameSession, job.session_id)
            if sess.state == "transcribing":
                sess.progress = job.progress
                sess.state_updated_at = utcnow()


def fail_job(db: Session, job: Job, code: str, message: str, retryable: bool) -> None:
    """Fehlschlag: bei vorübergehenden Fehlern neu einreihen, sonst Session auf failed."""
    job.error_code, job.error_message = code, message
    job.lease_expires_at = None
    if retryable and job.attempts < get_settings().max_attempts:
        job.state, job.lease_worker_id = "queued", None
        if job.session_id and job.type == "transcribe":
            set_state(db.get(GameSession, job.session_id), "queued", progress=0.0)
        elif job.session_id and job.type == "summarize":
            set_state(db.get(GameSession, job.session_id), "summarizing")
        elif job.type == "document":
            _dokument_zustand(db, job, "queued", None)
        return
    job.state = "failed"
    job.finished_at = utcnow()
    if job.type == "voice_enroll":
        from app.stimmprofile import fehlgeschlagen

        fehlgeschlagen(db, job, message)
    if job.type == "document":
        _dokument_zustand(db, job, "failed", status_message("doc.failed", detail=message))
    if job.session_id:
        sess = db.get(GameSession, job.session_id)
        key = {"transcribe": "status.failed_transcription",
               "summarize": "status.failed_summary"}.get(job.type, "status.failed_generic")
        set_state(sess, "failed", message=status_message(key, detail=message))


def _dokument_zustand(db: Session, job: Job, zustand: str, meldung: str | None) -> None:
    from app.models import CampaignDocument

    doc = db.get(CampaignDocument, job.document_id or "")
    if doc is not None:
        doc.state, doc.progress = zustand, None
        if meldung:
            doc.message = meldung


def sweep(db: Session) -> dict:
    """Wartung (läuft regelmäßig im Server): abgelaufene Leases, alte Audios, verwaiste Uploads."""
    s = get_settings()
    now = utcnow()
    ergebnis = {"leases": 0, "audio": 0, "uploads": 0}
    # 1. Leases ohne Lebenszeichen zurückholen
    for job in db.scalars(select(Job).where(Job.state == "leased", Job.lease_expires_at < now)):
        fail_job(db, job, "lease_expired", "Der Worker hat sich nicht mehr gemeldet.", retryable=True)
        ergebnis["leases"] += 1
    # 2. Aufnahmen nach Frist löschen (bis zur Freigabe: höchstens maxDays; sonst nur noch fehlgeschlagene)
    from app.aufbewahrung import abgelaufene_loeschen

    ergebnis["audio"] = abgelaufene_loeschen(db, s.audio_retention_days)
    # 3. Nie abgeschlossene Uploads verwerfen
    grenze = now - timedelta(days=s.upload_abandon_days)
    for up in db.scalars(select(Upload).where(Upload.state == "open", Upload.created_at < grenze)):
        storage.delete_upload_files(up.id)
        up.state = "aborted"
        sess = db.get(GameSession, up.session_id)
        if sess and sess.state == "uploading":
            set_state(sess, "created")
        ergebnis["uploads"] += 1
    # 4. Aufnahmen für Stimmprofile, deren Auftrag erledigt oder zu alt ist
    from app.stimmprofile import verwaiste_aufnahmen_loeschen

    ergebnis["stimmaufnahmen"] = verwaiste_aufnahmen_loeschen(db)
    db.commit()
    return ergebnis


def neu_starten(db: Session, s: GameSession) -> Job:
    """„Erneut versuchen“ (App und Verwaltung): setzt dort an, wo es schiefging.

    Zusammenfassung gescheitert → nur sie neu (Transkript ist da). Transkription gescheitert → neu, solange das
    Audio noch da ist, sonst 409 audio_gone."""
    from app import errors

    if s.state != "failed":
        raise errors.conflict("invalid_state")
    job = current_job(db, s.id, None)
    if job is not None and job.type == "summarize":
        return create_summarize_job(db, s)
    if job is None or job.state != "failed":
        raise errors.conflict("audio_gone")
    up = db.get(Upload, job.upload_id) if job.upload_id else None
    if up is None or s.audio_deleted_at is not None or not storage.upload_dir(up.id).exists():
        raise errors.conflict("audio_gone")
    return create_transcribe_job(db, s, up)
