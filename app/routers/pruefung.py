"""Nach der Transkription: Stimmen bestätigen, Recap und Vorschläge prüfen, veröffentlichen.

Spoilerschutz: Vorschläge und der Recap vor der Veröffentlichung sind nur für die SL. Spieler bekommen dafür immer
404 – auch bei veröffentlichten Sessions, damit nicht einmal die Existenz von Vorschlägen erkennbar ist.
"""
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, queue, schemas, stimmprofile
from app.access import SessionAccess, current_user, load_session
from app.db import get_db, utcnow
from app.errors import sprache
from app.models import Entry, GameSession, Member, Proposal, Recap, Speaker, User
from app.services import apply_entry_input, processing_status, session_out, set_state
from app.zusammenfassung import erwaehnt

router = APIRouter()

MAX_TEXT = 20000


def _nur_sl(db: Session, session_id: str, user: User, resource: str = "session") -> SessionAccess:
    """SL-Endpunkte rund um die Prüfung: Spieler bekommen 404 statt 403."""
    acc = load_session(db, session_id, user)
    if not acc.is_gm:
        raise errors.not_found(resource)
    return acc


def _pruefbar(s: GameSession) -> None:
    if s.state == "published":
        raise errors.conflict("already_published")
    if s.state != "awaiting_review":
        raise errors.conflict("invalid_state", "invalid_state.review")


def _kampagnen_mitglieder(db: Session, campaign_id: str) -> set[str]:
    return set(db.scalars(select(Member.id).where(Member.campaign_id == campaign_id)))


# ---------- Stimmen bestätigen ----------
@router.put("/sessions/{sessionId}/speakers", tags=["Stimmen"], status_code=202,
            response_model=schemas.ProcessingStatusOut)
def confirm_speakers(sessionId: str, body: list[schemas.SpeakerAssignIn], request: Request,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Zuordnung bestätigen. Nicht genannte Stimmen behalten den Vorschlag. Danach werden Hörproben und
    Stimmabdrücke gelöscht, und die Zusammenfassung wird eingereiht."""
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    if s.state != "awaiting_speakers":
        raise errors.conflict("invalid_state", "invalid_state.speakers")
    stimmen = {sp.id: sp for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))}
    ids = [z.speaker_id for z in body]
    if len(ids) != len(set(ids)):
        raise errors.bad_request("speaker_duplicate")
    if any(i not in stimmen for i in ids):
        raise errors.bad_request("speaker_unknown")
    mitglieder = _kampagnen_mitglieder(db, s.campaign_id)
    if any(z.member_id is not None and z.member_id not in mitglieder for z in body):
        raise errors.bad_request("member_unknown")
    angegeben = {z.speaker_id: z.member_id for z in body}
    for sp in stimmen.values():
        sp.assigned_member_id = angegeben[sp.id] if sp.id in angegeben else sp.suggested_member_id
    db.flush()
    stimmprofile.lernen(db, s)  # nur Profile mit „aus Sessions lernen“, bevor die Abdrücke gelöscht werden
    queue.stimmen_vergessen(db, s)
    queue.create_summarize_job(db, s)
    db.commit()
    return processing_status(db, s, sprache(request))


# ---------- Vorschläge ----------
def proposal_out(p: Proposal) -> schemas.ProposalOut:
    public = p.suggested_visibility == "public"
    return schemas.ProposalOut(
        id=p.id, session_id=p.session_id, document_id=p.document_id, entry_type=p.entry_type, action=p.action,
        target_entry_id=p.target_entry_id, title=p.title, detail=p.detail, gm_notes=p.gm_notes,
        suggested_visibility=p.suggested_visibility,
        hidden_from_member_ids=json.loads(p.hidden_member_ids_json) if public else [],
        public_suggested=p.public_suggested, visibility_reason=p.visibility_reason, confidence=p.confidence,
        flags=json.loads(p.flags), evidence=[schemas.EvidenceOut(**b) for b in json.loads(p.evidence)],
        decision=p.decision,
    )


@router.get("/sessions/{sessionId}/proposals", tags=["Vorschläge"], response_model=list[schemas.ProposalOut],
            response_model_exclude_unset=True)
def list_proposals(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _nur_sl(db, sessionId, user).session
    return [proposal_out(p) for p in db.scalars(
        select(Proposal).where(Proposal.session_id == s.id).order_by(Proposal.position))]


@router.patch("/proposals/{proposalId}", tags=["Vorschläge"], response_model=schemas.ProposalOut,
              response_model_exclude_unset=True)
def patch_proposal(proposalId: str, body: schemas.ProposalPatch, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    p = db.get(Proposal, proposalId)
    if p is None:
        raise errors.not_found()
    if p.session_id is not None:
        try:
            s = _nur_sl(db, p.session_id, user).session
        except errors.ApiError:
            raise errors.not_found() from None
        _pruefbar(s)
        campaign_id = s.campaign_id
    else:  # Vorschlag aus einer SL-Unterlage
        from app.access import membership
        from app.models import CampaignDocument

        doc = db.get(CampaignDocument, p.document_id or "")
        me = membership(db, doc.campaign_id, user) if doc else None
        if doc is None or me is None or me.role != "gm":
            raise errors.not_found()
        if doc.state != "awaiting_review":
            raise errors.conflict("invalid_state", "invalid_state.document_review")
        campaign_id = doc.campaign_id
    felder = body.model_fields_set
    if "title" in felder:
        titel = (body.title or "").strip()
        if not titel:
            raise errors.bad_request("validation_error", "validation_error.title")
        p.title = titel
    if "detail" in felder:
        p.detail = (body.detail or "").strip()
    if "gm_notes" in felder:
        p.gm_notes = (body.gm_notes or "").strip() or None
    if "visibility" in felder and body.visibility is not None:
        p.suggested_visibility = body.visibility
    if "hidden_from_member_ids" in felder:
        ids = sorted(set(body.hidden_from_member_ids or []))
        if not set(ids) <= _kampagnen_mitglieder(db, campaign_id):
            raise errors.bad_request("hidden_member_unknown")
        p.hidden_member_ids_json = json.dumps(ids)
    if "decision" in felder and body.decision is not None:
        p.decision = body.decision
    if p.decision == "accepted" and p.suggested_visibility == "public" and p.action != "update" and not p.detail:
        raise errors.bad_request("validation_error", "public_text_required")
    db.commit()
    return proposal_out(p)


# ---------- Recap ----------
def recap_out(s: GameSession, r: Recap) -> schemas.RecapOut:
    return schemas.RecapOut(session_id=s.id, number=s.number, title=r.title, text=r.text,
                            open_threads=json.loads(r.open_threads), published_at=s.published_at)


@router.get("/sessions/{sessionId}/recap", tags=["Chronik"], response_model=schemas.RecapOut)
def get_recap(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = load_session(db, sessionId, user).session  # Spieler: nur veröffentlichte Sessions
    r = db.get(Recap, s.id)
    if r is None:
        raise errors.not_found("recap")
    return recap_out(s, r)


@router.put("/sessions/{sessionId}/recap", tags=["Chronik"], response_model=schemas.RecapOut)
def put_recap(sessionId: str, body: schemas.RecapIn, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    r = db.get(Recap, s.id)
    if r is None and s.state != "published":
        raise errors.not_found("recap")
    _pruefbar(s)
    felder = body.model_fields_set
    if "title" in felder:
        titel = (body.title or "").strip()
        if not titel:
            raise errors.bad_request("validation_error", "validation_error.title")
        r.title = titel
    if "text" in felder:
        text = (body.text or "").strip()
        if not text:
            raise errors.bad_request("validation_error", "validation_error.recap_text")
        r.text = text
    if "open_threads" in felder:
        faeden = [f.strip() for f in (body.open_threads or []) if f.strip()]
        if any(len(f) > 1000 for f in faeden):
            raise errors.bad_request("validation_error", "validation_error.open_threads")
        r.open_threads = json.dumps(faeden, ensure_ascii=False)
    r.edited_at = utcnow()
    db.commit()
    return recap_out(s, r)


# ---------- Veröffentlichen ----------
def _anhaengen(alt: str | None, neu: str | None) -> str:
    return "\n\n".join(t for t in ((alt or "").strip(), (neu or "").strip()) if t)[:MAX_TEXT]


def _uebernehmen(db: Session, campaign_id: str, s: GameSession | None, p: Proposal, mitglieder: set[str]) -> None:
    """Einen angenommenen Vorschlag in die Bibel schreiben (nutzt dieselben Regeln wie PATCH /entries).
    s = Session, aus der er stammt (für „erwähnt in Kapitel …“); None bei Unterlagen."""
    public = p.suggested_visibility == "public"
    verborgen = [m for m in json.loads(p.hidden_member_ids_json) if m in mitglieder] if public else []
    if p.action == "create":
        e = Entry(campaign_id=campaign_id, type=p.entry_type, name=p.title)
        db.add(e)
        db.flush()
        daten = dict(type=p.entry_type, name=p.title, summary=p.detail[:MAX_TEXT], gm_notes=p.gm_notes or "",
                     visibility=p.suggested_visibility, hidden_from_member_ids=verborgen)
        notiz = p.detail if public else ""
    else:
        e = db.get(Entry, p.target_entry_id or "")
        if e is None or e.campaign_id != campaign_id:
            return  # Eintrag inzwischen gelöscht
        if p.action == "reveal":
            daten = dict(summary=p.detail[:MAX_TEXT], gm_notes=p.gm_notes or "", visibility=p.suggested_visibility)
            if public:
                daten["hidden_from_member_ids"] = verborgen
            notiz = p.detail if public else ""
        elif e.visibility == "public" and not public:
            # Geheimes Neues zu einem öffentlichen Eintrag landet nur im geheimen Teil
            daten = dict(gm_notes=_anhaengen(_anhaengen(e.gm_notes, p.detail), p.gm_notes))
            notiz = ""
        else:
            daten = dict(summary=_anhaengen(e.summary, p.detail), gm_notes=_anhaengen(e.gm_notes, p.gm_notes))
            if public and verborgen and e.visibility == "public":
                # 0.3.7: gilt auch für update – vereinigen, damit niemand versehentlich wieder alles sieht
                daten["hidden_from_member_ids"] = sorted(set(e.hidden_member_ids) | set(verborgen))
            notiz = p.detail if e.visibility == "public" else ""
    apply_entry_input(db, e, schemas.EntryInput(**daten))
    if s is not None:
        erwaehnt(db, e, s, notiz)


@router.post("/sessions/{sessionId}/publish", tags=["Chronik"], response_model=schemas.SessionOut,
             response_model_exclude_unset=True)
def publish(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    if s.state == "published":
        raise errors.conflict("already_published")
    r = db.get(Recap, s.id)
    if s.state != "awaiting_review" or r is None:
        raise errors.conflict("not_ready")
    mitglieder = _kampagnen_mitglieder(db, s.campaign_id)
    for p in db.scalars(select(Proposal).where(Proposal.session_id == s.id).order_by(Proposal.position)):
        if p.decision == "accepted":
            _uebernehmen(db, s.campaign_id, s, p, mitglieder)
        elif p.decision == "open":
            p.decision = "rejected"  # laut Schnittstelle: offene gelten als verworfen
    if not s.title:
        s.title = r.title
    s.published_at = utcnow()
    set_state(s, "published")
    db.commit()
    db.refresh(s)
    return session_out(s)
