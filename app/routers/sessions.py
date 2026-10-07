"""Sessions (Kapitel), Status, Neustart, Hörproben, Transkript und SL-Notiz.

Stimmen bestätigen, Recap, Vorschläge und Veröffentlichen: routers/pruefung.py
"""
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, queue, schemas, storage
from app.access import current_user, load_session, load_session_gm, require_gm, require_member
from app.db import get_db, utcnow
from app.errors import sprache
from app.models import Campaign, GameSession, GmNote, SessionSeen, Speaker, TranscriptSegment, Upload, User
from app.services import (
    build_attendees, log_on_site_consents, next_session_number, processing_status,
    session_out, session_summary_out,
)

router = APIRouter()


# ---------- Sessions ----------
@router.get("/campaigns/{campaignId}/sessions", tags=["Sessions"], response_model=list[schemas.SessionSummaryOut])
def list_sessions(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    q = select(GameSession).where(GameSession.campaign_id == campaignId)
    if me.role != "gm":
        q = q.where(GameSession.state == "published")
    sitzungen = list(db.scalars(q.order_by(GameSession.number)))
    from app.routers.miteinander import ungelesene_kommentare

    ungelesen = ungelesene_kommentare(db, me, sitzungen)
    return [session_summary_out(s, ungelesen.get(s.id, 0)) for s in sitzungen]


@router.post("/campaigns/{campaignId}/sessions", tags=["Sessions"], status_code=201, response_model=schemas.SessionOut,
             response_model_exclude_unset=True)
def create_session(
    campaignId: str, body: schemas.SessionCreate, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    me = require_member(db, campaignId, user)
    require_gm(me)
    if db.get(Campaign, campaignId).archived_at is not None:
        raise errors.conflict("campaign_archived")
    s = GameSession(
        campaign_id=campaignId, number=next_session_number(db, campaignId),
        title=(body.title or "").strip() or None, played_at=body.played_at, state="created",
    )
    s.attendees = build_attendees(db, campaignId, body.attendees)
    db.add(s)
    db.flush()
    log_on_site_consents(db, s, user)
    db.commit()
    db.refresh(s)
    return session_out(s)


@router.delete("/sessions/{sessionId}", tags=["Sessions"], status_code=204)
def delete_session(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Unveröffentlichtes Kapitel verwerfen (0.4.5): Session mit Aufnahme, Transkript, Hörproben, Vorschlägen und
    Recap-Entwurf; laufende Aufträge verschwinden mit (ein Worker bekommt dann 404 und bricht ab)."""
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    if s.state == "published":
        raise errors.conflict("session_published")
    for up in db.scalars(select(Upload).where(Upload.session_id == s.id)):
        storage.delete_upload_files(up.id)
    storage.delete_samples(s.id)
    campaign_id, nummer = s.campaign_id, s.number
    db.delete(s)
    db.flush()
    # Die Nummer wird wieder frei: spätere Kapitel rücken auf (aufsteigend, damit die Eindeutigkeit hält)
    for spaeter in db.scalars(select(GameSession).where(GameSession.campaign_id == campaign_id,
                                                        GameSession.number > nummer).order_by(GameSession.number)):
        spaeter.number -= 1
        db.flush()
    db.commit()
    return Response(status_code=204)


@router.get("/sessions/{sessionId}", tags=["Sessions"], response_model=schemas.SessionOut,
            response_model_exclude_unset=True)
def get_session(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    return session_out(acc.session, db, acc.member)


@router.patch("/sessions/{sessionId}", tags=["Sessions"], response_model=schemas.SessionOut,
              response_model_exclude_unset=True)
def patch_session(
    sessionId: str, body: schemas.SessionPatch, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    s = load_session_gm(db, sessionId, user).session
    fields = body.model_fields_set
    if "attendees" in fields and body.attendees is not None:
        if s.state != "created":
            raise errors.conflict("attendees_locked")
        neu = build_attendees(db, s.campaign_id, body.attendees)
        s.attendees.clear()
        db.flush()
        s.attendees.extend(neu)
        db.flush()
        log_on_site_consents(db, s, user)
    if "title" in fields:
        s.title = (body.title or "").strip() or None
    db.commit()
    db.refresh(s)
    return session_out(s)


@router.get("/sessions/{sessionId}/status", tags=["Sessions"], response_model=schemas.ProcessingStatusOut)
def get_status(sessionId: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return processing_status(db, load_session(db, sessionId, user).session, sprache(request))


@router.post("/sessions/{sessionId}/retry", tags=["Sessions"], status_code=202, response_model=schemas.ProcessingStatusOut)
def retry(sessionId: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session_gm(db, sessionId, user).session
    queue.neu_starten(db, s)
    db.commit()
    return processing_status(db, s, sprache(request))


@router.post("/sessions/{sessionId}/seen", tags=["Kommentare"], status_code=204)
def session_seen(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    row = db.get(SessionSeen, (acc.member.id, acc.session.id))
    if row is None:
        db.add(SessionSeen(member_id=acc.member.id, session_id=acc.session.id, seen_at=utcnow()))
    else:
        row.seen_at = utcnow()
    db.commit()
    return Response(status_code=204)


# ---------- Stimmen ----------
BESTAETIGT = ("summarizing", "awaiting_review", "published", "failed")


@router.get("/sessions/{sessionId}/speakers", tags=["Stimmen"], response_model=list[schemas.SpeakerOut],
            response_model_exclude_unset=True)
def list_speakers(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Auch nach der Bestätigung (0.4.10): dann mit assignedMemberId; vorher fehlt das Feld."""
    s = load_session_gm(db, sessionId, user).session
    bestaetigt = s.state in BESTAETIGT
    aus = []
    for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id).order_by(Speaker.position)):
        daten = dict(id=sp.id, label=sp.label, speaking_seconds=sp.speaking_seconds, sample_text=sp.sample_text,
                     suggested_member_id=sp.suggested_member_id, confidence=sp.confidence, source=sp.source,
                     assigned_guest_name=sp.assigned_guest_name)
        if bestaetigt:
            daten["assigned_member_id"] = sp.assigned_member_id
        aus.append(schemas.SpeakerOut(**daten))
    return aus


@router.get("/sessions/{sessionId}/speakers/{speakerId}/sample", tags=["Stimmen"])
def speaker_sample(sessionId: str, speakerId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session_gm(db, sessionId, user).session
    sp = db.get(Speaker, speakerId)
    if sp is None or sp.session_id != s.id:
        raise errors.not_found()
    pfad = storage.sample_path(s.id, sp.id)
    if not pfad.exists():
        raise errors.ApiError(410, "audio_deleted")  # seit 0.3.8 mit Fehlerkörper
    return FileResponse(pfad, media_type="audio/ogg")


@router.get("/sessions/{sessionId}/gm-note", tags=["Chronik"], response_model=schemas.GmNoteOut)
def get_gm_note(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session_gm(db, sessionId, user).session
    note = db.get(GmNote, s.id)
    if note is None:
        return schemas.GmNoteOut(text="", updated_at=s.created_at)
    return schemas.GmNoteOut(text=note.text, updated_at=note.updated_at)


@router.put("/sessions/{sessionId}/gm-note", tags=["Chronik"], status_code=204)
def put_gm_note(
    sessionId: str, body: schemas.GmNoteIn, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    s = load_session_gm(db, sessionId, user).session
    note = db.get(GmNote, s.id)
    if note is None:
        note = GmNote(session_id=s.id)
        db.add(note)
    note.text = body.text
    note.updated_at = utcnow()
    db.commit()
    return Response(status_code=204)


@router.get("/sessions/{sessionId}/transcript", tags=["Chronik"], response_model=list[schemas.TranscriptLineOut])
def get_transcript(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session_gm(db, sessionId, user).session
    sprecher = {sp.id: sp for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))}
    zeilen = []
    for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)
                          .order_by(TranscriptSegment.position)):
        sp = sprecher.get(seg.speaker_id)
        zeilen.append(schemas.TranscriptLineOut(
            start=seg.start, end=seg.end, speaker_id=seg.speaker_id or "", text=seg.text,
            member_id=sp.assigned_member_id if sp else None))
    return zeilen
