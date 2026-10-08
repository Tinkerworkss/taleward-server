"""SL-Unterlagen (nur SL). Spieler sehen in der Liste nur ihre eigenen Charakterbögen (0.4.7, vorher 403), fremde
Unterlagen bekommen sie nie (404) – schon deren Existenz ist SL-Wissen. Logik in app/unterlagen.py."""
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors, schemas, unterlagen
from app.access import current_user, membership, require_gm, require_member
from app.db import get_db
from app.errors import sprache
from app.models import CampaignDocument, Proposal, User
from app.routers.pruefung import proposal_out
from app.services import render_status_message

router = APIRouter(tags=["Unterlagen"])


def dokument_out(db: Session, doc: CampaignDocument, lang: str) -> schemas.CampaignDocumentOut:
    from app import queue
    from app.einstellungen import llm_konfig

    zaehlen = dict(db.execute(select(Proposal.decision, func.count()).where(Proposal.document_id == doc.id)
                              .group_by(Proposal.decision)).all())
    meldung = render_status_message(doc.message, lang)
    if doc.state == "queued":
        k = llm_konfig(db)
        if k.art == "lokal" and not queue.llm_worker_online(db):
            meldung = errors.ApiError(0, "doc.waiting_worker").message(lang)
        elif k.art != "lokal" and not k.bereit:
            meldung = errors.ApiError(0, "doc.waiting_llm").message(lang)
        else:
            from app import kosten

            from app.models import Campaign
            from app.services import cloud_anbieter_name

            c = db.get(Campaign, doc.campaign_id)
            if k.art == "api" and not (c and c.allow_cloud_summary):
                meldung = errors.ApiError(0, "status.cloud_summary_not_allowed",
                                          anbieter=cloud_anbieter_name(k)).message(lang)
            elif k.art == "api" and kosten.erreicht(db):
                meldung = errors.ApiError(0, "status.cost_limit").message(lang)
    return schemas.CampaignDocumentOut(
        id=doc.id, campaign_id=doc.campaign_id, title=doc.title, file_name=doc.file_name, kind=doc.kind,
        size_bytes=doc.size_bytes, page_count=doc.page_count, state=doc.state, progress=doc.progress,
        message=meldung, proposal_count=sum(zaehlen.values()), open_proposal_count=zaehlen.get("open", 0),
        world_info_suggestion=doc.world_info_suggestion, uploaded_by_member_id=doc.uploaded_by_member_id,
        created_at=doc.created_at,
    )


def _dokument(db: Session, document_id: str, user: User, eigener_bogen: bool = False) -> CampaignDocument:
    """Nur für die SL der Kampagne – alle anderen bekommen 404. eigener_bogen: auch die Person, die den Charakterbogen
    hochgeladen hat (0.4.7)."""
    doc = db.get(CampaignDocument, document_id)
    me = membership(db, doc.campaign_id, user) if doc else None
    if doc is None or me is None:
        raise errors.not_found("document")
    if me.role != "gm" and not (eigener_bogen and doc.kind == "character_sheet" and doc.uploaded_by_member_id == me.id):
        raise errors.not_found("document")
    return doc


@router.get("/campaigns/{campaignId}/documents", response_model=list[schemas.CampaignDocumentOut])
def documents_list(campaignId: str, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    stmt = select(CampaignDocument).where(CampaignDocument.campaign_id == campaignId)
    if me.role != "gm":  # Spieler: nur die eigenen Charakterbögen
        stmt = stmt.where(CampaignDocument.kind == "character_sheet", CampaignDocument.uploaded_by_member_id == me.id)
    docs = db.scalars(stmt.order_by(CampaignDocument.created_at.desc()))
    return [dokument_out(db, d, sprache(request)) for d in docs]


@router.post("/campaigns/{campaignId}/documents", status_code=201, response_model=schemas.CampaignDocumentOut)
def documents_upload(campaignId: str, request: Request, file: UploadFile = File(...), kind: str = Form(...),
                     title: str | None = Form(None), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    if kind != "character_sheet":  # Charakterbögen darf jedes aktive Mitglied hochladen (0.4.7)
        require_gm(me)
    daten = file.file.read(unterlagen.MAX_BYTES + 1)
    doc = unterlagen.hochladen(db, campaignId, me.id, file.filename or "", daten, kind, title)
    db.commit()
    db.refresh(doc)
    return dokument_out(db, doc, sprache(request))


@router.get("/documents/{documentId}", response_model=schemas.CampaignDocumentOut)
def document_get(documentId: str, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    return dokument_out(db, _dokument(db, documentId, user, eigener_bogen=True), sprache(request))


@router.delete("/documents/{documentId}", status_code=204)
def document_delete(documentId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    unterlagen.loeschen(db, _dokument(db, documentId, user, eigener_bogen=True))
    db.commit()
    return Response(status_code=204)


# 0.4.12: Nachlesen während der Runde (SL-Schirm). Rechte wie GET /documents/{id}: SL, beim Charakterbogen auch die
# Urheberin. Die Datei kommt als Anzeige im Browser, aber abgeschottet: Kein Skript in einer Unterlage läuft im
# Ursprung des Servers (CSP sandbox), der Typ wird nicht erraten (nosniff), nichts wird zwischengespeichert.
MEDIENTYP = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
             ".markdown": "text/plain; charset=utf-8",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


@router.get("/documents/{documentId}/file")
def document_file(documentId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from urllib.parse import quote

    doc = _dokument(db, documentId, user, eigener_bogen=True)
    pfad = unterlagen.datei_pfad(doc.id)
    if pfad is None:
        raise errors.not_found("document")
    name = doc.file_name or pfad.name
    einfach = "".join(z if 32 <= ord(z) < 127 and z not in '"\\' else "_" for z in name) or "unterlage"
    return Response(pfad.read_bytes(), media_type=MEDIENTYP.get(pfad.suffix.lower(), "application/octet-stream"),
                    headers={"Content-Disposition": f'inline; filename="{einfach}"; filename*=UTF-8\'\'{quote(name)}',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
                                                        "img-src data:; object-src 'none'"})


@router.get("/documents/{documentId}/text", response_model=schemas.DocumentTextOut)
def document_text(documentId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _dokument(db, documentId, user, eigener_bogen=True)
    seiten = []
    for i, a in enumerate(unterlagen.text_laden(doc.id)):
        if not isinstance(a, dict) or not str(a.get("text") or "").strip():
            continue
        try:
            nummer = int(a.get("seite") or 0)
        except (TypeError, ValueError):
            nummer = 0
        seiten.append(schemas.DocumentPageOut(page=nummer if nummer >= 1 else i + 1, text=str(a["text"])))
    return schemas.DocumentTextOut(pages=seiten)


@router.get("/documents/{documentId}/proposals", response_model=list[schemas.ProposalOut],
            response_model_exclude_unset=True)
def document_proposals(documentId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _dokument(db, documentId, user)
    return [proposal_out(p) for p in db.scalars(
        select(Proposal).where(Proposal.document_id == doc.id).order_by(Proposal.position))]


@router.post("/documents/{documentId}/apply", response_model=schemas.CampaignDocumentOut)
def document_apply(documentId: str, request: Request, body: schemas.DocumentApplyIn | None = None,
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _dokument(db, documentId, user)
    unterlagen.uebernehmen(db, doc, bool(body and body.apply_world_info))
    db.commit()
    db.refresh(doc)
    return dokument_out(db, doc, sprache(request))


@router.post("/documents/{documentId}/retry", status_code=202, response_model=schemas.CampaignDocumentOut)
def document_retry(documentId: str, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    doc = _dokument(db, documentId, user)
    unterlagen.neu_starten(db, doc)
    db.commit()
    db.refresh(doc)
    return dokument_out(db, doc, sprache(request))
