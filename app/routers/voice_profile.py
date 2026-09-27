"""Stimmprofil (Schritt 6): freiwillig, eigene ausdrückliche Einwilligung, nur der Stimmabdruck wird gespeichert.

Ablauf: Aufnahme (20–30 s) → Auftrag für einen Worker → Abdruck zurück → Audio sofort gelöscht → „ready“.
Löschen entfernt Profil, alles Gelernte, offene Aufträge und eine noch nicht verarbeitete Aufnahme.
"""
import shutil
import subprocess
import tempfile

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy.orm import Session

from app import errors, queue, schemas, stimmprofile
from app.access import current_user
from app.db import get_db
from app.errors import sprache
from app.models import User, VoiceProfile

router = APIRouter(tags=["Stimmprofil"])

MIN_AUFNAHME_SEKUNDEN = 10.0


def _out(db: Session, vp: VoiceProfile | None, lang: str = "de") -> schemas.VoiceProfileOut:
    if vp is None:
        return schemas.VoiceProfileOut(
            status="none", created_at=None, sample_seconds=None,
            learn_from_sessions=False, learned_session_count=0, message=None,
        )
    message = vp.message
    if vp.status == "processing" and not queue.worker_online(db, "asr"):
        message = errors.ApiError(0, "status.no_worker").message(lang)
    return schemas.VoiceProfileOut(
        status=vp.status, created_at=vp.created_at, sample_seconds=vp.sample_seconds,
        learn_from_sessions=vp.learn_from_sessions, learned_session_count=vp.learned_session_count,
        message=message,
    )


def _dauer(daten: bytes) -> float | None:
    """Länge der Aufnahme per ffprobe, falls vorhanden (sonst prüft der Worker)."""
    if shutil.which("ffprobe") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".audio") as f:
        f.write(daten)
        f.flush()
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name],
                             capture_output=True, text=True, timeout=20)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


@router.get("/me/voice-profile", response_model=schemas.VoiceProfileOut)
def get_profile(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _out(db, db.get(VoiceProfile, user.id), sprache(request))


@router.post("/me/voice-profile", status_code=202, response_model=schemas.VoiceProfileOut)
async def create_profile(
    request: Request,
    audio: UploadFile | None = File(None),
    consent: str | None = Form(None),
    learn_from_sessions: str | None = Form(None, alias="learnFromSessions"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if consent != "true":
        raise errors.bad_request("voice_consent_missing")
    if audio is None:
        raise errors.bad_request("voice_audio_missing")
    typ = (audio.content_type or "").lower()
    if typ and not typ.startswith(stimmprofile.AUDIO_TYPEN):
        raise errors.bad_request("voice_unsupported")
    daten = await audio.read(stimmprofile.MAX_BYTES + 1)
    if len(daten) > stimmprofile.MAX_BYTES:
        raise errors.ApiError(413, "voice_too_large")
    if not daten:
        raise errors.bad_request("voice_audio_missing")
    vp = db.get(VoiceProfile, user.id)
    if vp is not None and vp.status == "processing":
        raise errors.conflict("voice_processing")
    dauer = _dauer(daten)
    if dauer is not None and dauer < MIN_AUFNAHME_SEKUNDEN:
        raise errors.bad_request("voice_too_short", "voice_too_short", sekunden=int(dauer))
    lernen = None if learn_from_sessions is None else learn_from_sessions == "true"
    vp = stimmprofile.anlegen(db, user.id, daten, lernen)
    db.commit()
    return _out(db, vp, sprache(request))


@router.patch("/me/voice-profile", response_model=schemas.VoiceProfileOut)
def patch_profile(request: Request, body: schemas.VoiceProfilePatch, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    vp = db.get(VoiceProfile, user.id)
    if vp is None:
        vp = VoiceProfile(user_id=user.id, status="none", learn_from_sessions=False, learned_session_count=0)
        db.add(vp)
    if body.learn_from_sessions is not None:
        vp.learn_from_sessions = body.learn_from_sessions
    db.commit()
    return _out(db, vp, sprache(request))


@router.delete("/me/voice-profile", status_code=204)
def delete_profile(user: User = Depends(current_user), db: Session = Depends(get_db)):
    stimmprofile.loeschen(db, user.id)
    db.commit()
    return Response(status_code=204)
