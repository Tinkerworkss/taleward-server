"""Kampagnen-Umzug (Schnittstelle 0.4.8): Zustimmung, Export, Import, offene Plätze. Logik in app/umzug.py."""
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, schemas, umzug
from app.access import _bearer, current_user, membership, require_gm, require_member
from app.db import get_db
from app.models import Campaign, CampaignExport, Member, User
from app.services import campaign_out, member_out, set_move_consent

router = APIRouter(tags=["Umzug"])


@router.put("/campaigns/{campaignId}/members/me/move-consent", response_model=schemas.MemberOut,
            response_model_exclude_unset=True)
def move_consent(campaignId: str, body: schemas.MoveConsentIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    set_move_consent(db, me, body.granted)
    db.commit()
    db.refresh(me)
    return member_out(me, me)


# ---------------------------------------------------------------- Export
def _nur_sl(db: Session, campaign_id: str, user: User):
    """Exporte gibt es für Spieler nicht – auch nicht als 403."""
    me = require_member(db, campaign_id, user)
    if me.role != "gm":
        raise errors.not_found()
    return db.get(Campaign, campaign_id), me


def _export(db: Session, campaign_id: str, export_id: str) -> CampaignExport:
    x = db.get(CampaignExport, export_id)
    if x is None or x.campaign_id != campaign_id:
        raise errors.not_found("export")
    return x


@router.post("/campaigns/{campaignId}/exports", status_code=202, response_model=schemas.CampaignExportOut)
def start_export(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c, _ = _nur_sl(db, campaignId, user)
    x = umzug.export_starten(db, c, user)
    umzug.export_fortsetzen(x.id)
    db.refresh(x)
    return umzug.export_out(x)


@router.get("/campaigns/{campaignId}/exports/{exportId}", response_model=schemas.CampaignExportOut)
def get_export(campaignId: str, exportId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _nur_sl(db, campaignId, user)
    return umzug.export_out(_export(db, campaignId, exportId))


@router.get("/campaigns/{campaignId}/exports/{exportId}/file")
def export_file(campaignId: str, exportId: str, t: str | None = Query(default=None),
                creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)):
    """Mit Bearer-Token der SL oder mit dem Download-Schlüssel t (Browser, Android-Downloadmanager). Range-Anfragen
    beantwortet FileResponse selbst (206)."""
    x = db.get(CampaignExport, exportId)
    if t is not None:
        if x is None or x.campaign_id != campaignId or not umzug.schluessel_passt(x.id, t):
            raise errors.not_found("export")
        # Der Schlüssel gilt nur, solange die Anforderin noch SL der Kampagne ist
        sl = db.scalar(select(Member).where(Member.campaign_id == campaignId,
                                            Member.user_id == x.requested_by_user_id, Member.role == "gm",
                                            Member.left_at.is_(None)).limit(1)) if x.requested_by_user_id else None
        if sl is None:
            raise errors.not_found("export")
    else:
        user = current_user(creds, db)
        _nur_sl(db, campaignId, user)
        x = _export(db, campaignId, exportId)
    if x.state != "ready":
        raise errors.not_found("export")
    if not umzug.verfuegbar(x):
        raise errors.ApiError(410, "export_expired")
    return FileResponse(umzug.export_pfad(x.id), media_type="application/zip", filename=x.file_name,
                        headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- Import
@router.post("/imports", status_code=201, response_model=schemas.ImportStartOut)
def start_import(body: schemas.ImportStartIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    imp = umzug.import_beginnen(db, user, body)
    return schemas.ImportStartOut(import_id=imp.id, chunk_size_bytes=imp.chunk_size, chunk_count=imp.chunk_count)


@router.get("/imports/{importId}", response_model=schemas.ImportStatusOut, response_model_exclude_unset=True)
def get_import(importId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return umzug.import_out(umzug.laden(db, importId, user))


@router.put("/imports/{importId}/chunks/{index}", status_code=204)
async def put_import_chunk(importId: str, index: int, request: Request,
                           x_chunk_sha256: str | None = Header(default=None),
                           user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.koerper import lesen

    imp = umzug.laden(db, importId, user)
    umzug.teil_speichern(imp, index, await lesen(request, imp.chunk_size), x_chunk_sha256)
    return Response(status_code=204)


@router.post("/imports/{importId}/complete", status_code=202, response_model=schemas.ImportStatusOut,
             response_model_exclude_unset=True)
def complete_import(importId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    imp = umzug.laden(db, importId, user)
    umzug.import_abschliessen(db, imp)
    if imp.state == "processing":
        umzug.import_fortsetzen(imp.id)
    db.refresh(imp)
    return umzug.import_out(imp)


# ---------------------------------------------------------------- Offene Plätze
@router.post("/campaigns/{campaignId}/members/{memberId}/invite", status_code=201,
             response_model=schemas.SeatInviteOut)
def seat_invite(campaignId: str, memberId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    require_gm(me)
    platz = umzug.offener_platz(db, campaignId, memberId)
    inv = umzug.platz_einladung(db, platz, user)
    db.commit()
    return schemas.SeatInviteOut(code=inv.code, expires_at=inv.expires_at, member_id=platz.id)


@router.post("/campaigns/{campaignId}/members/{memberId}/take", response_model=schemas.CampaignOut,
             response_model_exclude_unset=True)
def take_seat(campaignId: str, memberId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    require_gm(me)
    c = db.get(Campaign, campaignId)
    if c.imported_by_member_id is None or me.id != c.imported_by_member_id:
        raise errors.ApiError(403, "not_importer")
    platz = umzug.offener_platz(db, campaignId, memberId)
    umzug.nehmen(db, c, me, platz)
    db.commit()
    db.refresh(c)
    return campaign_out(db, c, membership(db, campaignId, user))


@router.post("/campaigns/{campaignId}/members/{memberId}/release", response_model=schemas.MemberOut,
             response_model_exclude_unset=True)
def release_seat(campaignId: str, memberId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    require_gm(me)
    platz = db.get(type(me), memberId)
    if platz is None or platz.campaign_id != campaignId:
        raise errors.not_found("member")
    umzug.freigeben(db, platz)
    db.commit()
    db.refresh(platz)
    sl = membership(db, campaignId, user)
    return member_out(platz, sl if sl is not None else me)
