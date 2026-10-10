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


NEU_SCHREIBEN_JE_TAG = 5  # 0.4.62: Kapitel neu schreiben kostet bei der Cloud jedes Mal


def _neu_schreiben_pruefen(db: Session, s: GameSession) -> None:
    """Höchstens NEU_SCHREIBEN_JE_TAG Zusammenfassungen je Kapitel in 24 Stunden (409 resummarize_limit)."""
    from datetime import timedelta

    from sqlalchemy import func

    from app.models import Job

    seit = utcnow() - timedelta(hours=24)
    anzahl = db.scalar(select(func.count()).select_from(Job).where(
        Job.session_id == s.id, Job.type == "summarize", Job.created_at >= seit)) or 0
    if anzahl >= NEU_SCHREIBEN_JE_TAG:
        raise errors.conflict("resummarize_limit")


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
    nachtraeglich = s.state == "awaiting_review"  # 0.4.10: Zuordnung ändern und Kapitel neu schreiben
    if s.state != "awaiting_speakers" and not nachtraeglich:
        raise errors.conflict("wrong_state", "wrong_state.speakers")
    if nachtraeglich and not _hat_abschrift(db, s):
        raise errors.conflict("transcript_missing")
    if nachtraeglich:
        _neu_schreiben_pruefen(db, s)
    stimmen = {sp.id: sp for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))}
    ids = [z.speaker_id for z in body]
    if len(ids) != len(set(ids)):
        raise errors.bad_request("speaker_duplicate")
    if any(i not in stimmen for i in ids):
        raise errors.bad_request("speaker_unknown")
    mitglieder = _kampagnen_mitglieder(db, s.campaign_id)
    if any(z.member_id is not None and z.member_id not in mitglieder for z in body):
        raise errors.bad_request("member_unknown")
    # 0.4.7: Stimmen von Gästen benennen – nur mit einem guestName der Anwesenden, nie zusammen mit memberId
    gaeste = {" ".join(a.guest_name.split()).casefold(): a.guest_name for a in s.attendees if a.guest_name}
    gast = {}
    for z in body:
        name = " ".join((z.guest_name or "").split())
        if not name:
            continue
        if z.member_id is not None:
            raise errors.bad_request("speaker_member_and_guest")
        if name.casefold() not in gaeste:
            raise errors.bad_request("guest_unknown")
        gast[z.speaker_id] = gaeste[name.casefold()]
    angegeben = {z.speaker_id: z.member_id for z in body}
    for sp in stimmen.values():
        sp.assigned_member_id = angegeben[sp.id] if sp.id in angegeben else sp.suggested_member_id
        sp.assigned_guest_name = gast.get(sp.id)
    db.flush()
    if not nachtraeglich:  # nachträglich sind Hörproben und Abdrücke längst gelöscht
        stimmprofile.lernen(db, s)  # nur Profile mit „aus Sessions lernen“, bevor die Abdrücke gelöscht werden
        queue.stimmen_vergessen(db, s)
    if nachtraeglich:
        from app import korrektur

        korrektur.verwerfen(db, db.get(Recap, s.id))
    queue.create_summarize_job(db, s)
    db.commit()
    return processing_status(db, s, sprache(request))


def _hat_abschrift(db: Session, s: GameSession) -> bool:
    from app.models import TranscriptSegment

    return db.scalar(select(TranscriptSegment.id).where(TranscriptSegment.session_id == s.id).limit(1)) is not None


@router.post("/sessions/{sessionId}/resummarize", tags=["Sessions"], status_code=202,
             response_model=schemas.ProcessingStatusOut)
def resummarize(sessionId: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """0.4.10: Kapitel aus der vorhandenen Abschrift neu schreiben (nur SL, nur awaiting_review).

    Recap, offene Fäden, Vorschläge und Prüfteil entstehen neu (Speichern ersetzt sie); Bearbeitungen am Entwurf und
    Entscheidungen zu Vorschlägen fallen damit weg. SL-Notiz, Korrekturen, Stimmenzuordnung, Anwesenheit und
    Kommentare bleiben. Kosten und Cloud-Freigabe wie bei jeder Zusammenfassung."""
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    if s.state != "awaiting_review":
        raise errors.conflict("wrong_state", "wrong_state.resummarize")
    if not _hat_abschrift(db, s):
        raise errors.conflict("transcript_missing")
    _neu_schreiben_pruefen(db, s)
    from app import korrektur

    korrektur.verwerfen(db, db.get(Recap, s.id))  # 0.4.15: ein offener Korrektur-Entwurf passt nicht mehr
    queue.create_summarize_job(db, s)
    db.commit()
    return processing_status(db, s, sprache(request))


# ---------- Vorschläge ----------
def proposal_out(p: Proposal) -> schemas.ProposalOut:
    public = p.suggested_visibility == "public"
    quelle = p.source or ("session" if p.session_id else "document")
    return schemas.ProposalOut(
        id=p.id, session_id=p.session_id, document_id=p.document_id, source=quelle,
        origin_character_id=p.origin_character_id, origin_entry_id=p.origin_entry_id,
        submitted_by_member_id=p.submitted_by_member_id, entry_type=p.entry_type, action=p.action,
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
    vorher = p.decision
    if p.source == "character":  # mitgebrachte Welt (0.4.7): nur die SL, Entscheidung wirkt sofort
        from app.access import membership

        me = membership(db, p.campaign_id, user)
        if me is None or me.role != "gm":
            raise errors.not_found()
        if p.decision != "open":
            raise errors.conflict("invalid_state", "invalid_state.proposal_decided")
        campaign_id = p.campaign_id
    elif p.session_id is not None:
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
    if p.source == "character" and p.decision != vorher:
        from app import charaktere

        charaktere.entscheiden(db, p, _kampagnen_mitglieder(db, campaign_id))
    db.commit()
    return proposal_out(p)


# ---------- Recap ----------
def recap_out(s: GameSession, r: Recap, sl: bool = False, lang: str = "de") -> schemas.RecapOut:
    """Der Prüfteil (review, 0.4.6) nur für die SL – für Spieler gar nicht im JSON."""
    out = schemas.RecapOut(session_id=s.id, number=s.number, title=r.title, text=r.text,
                           open_threads=json.loads(r.open_threads), published_at=s.published_at)
    if sl:
        from app import korrektur, pruefteil

        out.review = pruefteil.lesen(r.review)
        out.revision = korrektur.fuer_app(r, lang)  # 0.4.15: null ohne Entwurf
    return out


@router.get("/sessions/{sessionId}/recap", tags=["Chronik"], response_model=schemas.RecapOut,
            response_model_exclude_unset=True)
def get_recap(sessionId: str, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)  # Spieler: nur veröffentlichte Sessions
    s = acc.session
    r = db.get(Recap, s.id)
    if r is None:
        raise errors.not_found("recap")
    return recap_out(s, r, acc.is_gm, sprache(request))


@router.put("/sessions/{sessionId}/recap", tags=["Chronik"], response_model=schemas.RecapOut,
            response_model_exclude_unset=True)
def put_recap(sessionId: str, body: schemas.RecapIn, request: Request, user: User = Depends(current_user),
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
    from app import korrektur

    text_neu = "text" in felder and (body.text or "").strip() != r.text.strip()
    if text_neu and (korrektur.lesen(r) or {}).get("state") == "running":
        raise errors.conflict("revision_running")  # 0.4.15
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
    if text_neu:
        korrektur.verwerfen(db, r)  # Bearbeiten von Hand verwirft einen offenen Entwurf
    if "open_threads" in felder:
        faeden = [f.strip() for f in (body.open_threads or []) if f.strip()]
        if any(len(f) > 1000 for f in faeden):
            raise errors.bad_request("validation_error", "validation_error.open_threads")
        r.open_threads = json.dumps(faeden, ensure_ascii=False)
    if "text" in felder:
        from app import pruefteil

        pruefung = pruefteil.lesen(r.review)
        if pruefung["state"] == "done" and not pruefung["stale"]:
            pruefung["stale"] = True  # Absätze können verrutscht sein – die App zeigt „vor deiner Änderung“
            r.review = pruefteil.als_json(pruefung)
    r.edited_at = utcnow()
    db.commit()
    return recap_out(s, r, True, sprache(request))


# ---------- Kapitel per Hinweis korrigieren (0.4.15) ----------
def _recap_fuer_sl(db: Session, sessionId: str, user: User) -> tuple[GameSession, Recap]:
    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.not_found() if acc.session.state != "published" else errors.forbidden()
    r = db.get(Recap, acc.session.id)
    if r is None:
        raise errors.not_found("recap")
    return acc.session, r


@router.post("/sessions/{sessionId}/recap/revision", tags=["Chronik"], status_code=202,
             response_model=schemas.RecapOut, response_model_exclude_unset=True)
def start_revision(sessionId: str, body: schemas.RevisionIn, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Die SL schreibt, was nicht stimmt oder fehlt; der Server legt einen Entwurf an (läuft im Hintergrund)."""
    from app import korrektur

    s, r = _recap_fuer_sl(db, sessionId, user)
    korrektur.starten(db, s, r, body.note, body.base_text)
    db.commit()
    return recap_out(s, r, True, sprache(request))


@router.post("/sessions/{sessionId}/recap/revision/decision", tags=["Chronik"], response_model=schemas.RecapOut,
             response_model_exclude_unset=True)
def decide_revision(sessionId: str, body: schemas.DecisionIn, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    from app import korrektur

    s, r = _recap_fuer_sl(db, sessionId, user)
    korrektur.entscheiden(db, s, r, body.accept)
    db.commit()
    return recap_out(s, r, True, sprache(request))


# ---------- Unsichere Namen (0.4.6) ----------
def _korrigierbar(db: Session, s: GameSession) -> bool:
    """awaiting_review, oder failed mit schon vorhandenem Transkript (Zusammenfassung gescheitert)."""
    from app.models import TranscriptSegment

    if s.state == "awaiting_review":
        return True
    return s.state == "failed" and db.scalar(
        select(TranscriptSegment.id).where(TranscriptSegment.session_id == s.id).limit(1)) is not None


@router.get("/sessions/{sessionId}/uncertain-terms", tags=["Chronik"])
def uncertain_terms(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app import unsicher

    s = _nur_sl(db, sessionId, user).session
    weg = unsicher.audio_weg_am(db, s)
    return {
        "audioAvailable": unsicher.audio_da(db, s),
        "audioDeletesAt": weg.isoformat().replace("+00:00", "Z") if weg else None,
        "retranscribesLeft": max(0, unsicher.MAX_NACHTRANSKRIPTIONEN - (s.nachtranskriptionen or 0)),
        "terms": unsicher.begriffe(db, s) if _korrigierbar(db, s) or s.state == "published" else [],
    }


@router.post("/sessions/{sessionId}/corrections", tags=["Chronik"])
def corrections(sessionId: str, body: schemas.CorrectionsIn, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    from fastapi.responses import JSONResponse

    from app import namenshilfe, queue, unsicher
    from app.models import Campaign, Upload

    acc = load_session(db, sessionId, user)
    if not acc.is_gm:
        raise errors.forbidden()
    s = acc.session
    if not _korrigierbar(db, s):
        raise errors.conflict("wrong_state")
    r = db.get(Recap, s.id)
    if not body.retranscribe and r is None:
        raise errors.conflict("wrong_state")
    if body.retranscribe:
        if not unsicher.audio_da(db, s):
            raise errors.conflict("audio_gone")
        if (s.nachtranskriptionen or 0) >= unsicher.MAX_NACHTRANSKRIPTIONEN:
            raise errors.conflict("retranscribe_limit")
    c = db.get(Campaign, s.campaign_id)
    paare = []
    for k in body.corrections:
        heard, correct = " ".join(k.heard.split()), " ".join(k.correct.split())
        if not heard:
            raise errors.bad_request("validation_error")
        if not correct:
            namenshilfe.ignorieren(c, heard)
            continue
        paare.append((heard, correct))
        if k.add_to_hotwords:
            namenshilfe.hinzufuegen(db, c, correct)
    if body.retranscribe:
        up = db.scalar(select(Upload).where(Upload.session_id == s.id, Upload.state == "completed")
                       .order_by(Upload.completed_at.desc()).limit(1))
        # Offene Aufträge dieser Session (z. B. eine laufende Zusammenfassung nach failed) beenden
        from app.models import Job

        for j in db.scalars(select(Job).where(Job.session_id == s.id, Job.state.in_(("queued", "leased")))):
            j.state, j.lease_expires_at, j.error_code = "failed", None, "superseded"
        s.nachtranskriptionen = (s.nachtranskriptionen or 0) + 1
        s.nachtranskription = True
        queue.create_transcribe_job(db, s, up)
        db.commit()
        return JSONResponse(status_code=202, content=processing_status(db, s, sprache(request))
                            .model_dump(mode="json", by_alias=True))
    unsicher.ersetzen(db, s, paare)
    db.commit()
    return recap_out(s, r, True, sprache(request))


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
    from app import korrektur

    korrektur.verwerfen(db, r)  # 0.4.15: ein offener Korrektur-Entwurf samt Hinweis bleibt nicht liegen
    s.published_at = utcnow()
    set_state(s, "published")
    from app.routers.plaene import gespielt_markieren

    gespielt_markieren(db, s.campaign_id, s.number)  # 0.4.12: Kapitelplan dieser Nummer gilt als gespielt
    from app.aufbewahrung import audio_loeschen

    audio_loeschen(db, s)  # Recap freigegeben: die Aufnahme wird nicht mehr gebraucht
    db.commit()
    db.refresh(s)
    return session_out(s)
