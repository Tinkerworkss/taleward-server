import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import charaktere, errors, schemas
from app.access import aktive_sl_anzahl, current_user, membership, require_gm, require_member
from app.db import get_db, utcnow
from app.models import Campaign, Invite, Member, UsageLog, User
from app.services import (
    campaign_out, campaign_summary, choose_organization, create_invite, ensure_org_member, member_out,
    normalize_invite_code, random_cover, set_recording_consent,
)

router = APIRouter(tags=["Kampagnen"])


def _load(db: Session, campaign_id: str, user: User) -> tuple[Campaign, Member]:
    me = require_member(db, campaign_id, user)
    return db.get(Campaign, campaign_id), me


@router.get("/campaigns", response_model=list[schemas.CampaignSummaryOut])
def list_campaigns(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Campaign, Member).join(Member, Member.campaign_id == Campaign.id)
        .where(Member.user_id == user.id, Member.left_at.is_(None))
        .order_by(Campaign.created_at.desc())
    ).all()
    return [campaign_summary(db, c, m) for c, m in rows]


@router.post("/campaigns", status_code=201, response_model=schemas.CampaignOut, response_model_exclude_unset=True)
def create_campaign(body: schemas.CampaignCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    title = body.title.strip()
    if not title:
        raise errors.bad_request("validation_error", "validation_error.title")
    org_id = choose_organization(db, user, body.organization_id)
    c = Campaign(
        title=title, description=body.description.strip(), organization_id=org_id, language=body.language,
        system=body.system, system_name=(body.system_name or "").strip() or None, cover_preset=random_cover(),
    )
    from app.einrichtung import betriebsart

    # Betriebsart „Nur Cloud“: ohne Cloud-Transkription ginge gar nichts – die SL kann es wieder ausschalten
    c.allow_external_transcription = betriebsart(db) == "cloud"
    c.allow_cloud_summary = betriebsart(db) == "cloud"  # 0.3.10: wie oben, die SL kann es ausschalten
    db.add(c)
    db.flush()
    me = Member(campaign_id=c.id, user_id=user.id, role="gm")
    db.add(me)
    db.commit()
    db.refresh(c)
    return campaign_out(db, c, me)


@router.post("/campaigns/join", response_model=schemas.CampaignOut, response_model_exclude_unset=True)
def join(body: schemas.JoinRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = db.get(Invite, normalize_invite_code(body.code))
    if inv is None or inv.expires_at <= utcnow():
        raise errors.ApiError(404, "invite_invalid")
    c = db.get(Campaign, inv.campaign_id)
    me = membership(db, c.id, user)
    char = (body.character_name or "").strip() or None
    if body.character is not None:  # 0.4.7: Charakter aus der Sammlung gewinnt gegen den freien Namen
        char = body.character.name.strip() or char
    neu = me is None
    if me is None:
        frueher = db.scalar(select(Member).where(Member.campaign_id == c.id, Member.user_id == user.id))
        if frueher is not None:  # verlassen oder entfernt: mit neuer Einladung wieder aktiv (0.4.5), als Spieler
            me = frueher
            me.left_at, me.role, me.joined_at = None, "player", utcnow()
            me.chronicle_seen_at = me.bible_seen_at = None
            if body.character is not None and me.character_id not in (None, body.character.id.lower()):
                charaktere.loesen(db, me)  # mit einem anderen Charakter zurück
            if char is not None:
                me.character_name = char
        else:
            me = Member(campaign_id=c.id, user_id=user.id, role="player", character_name=char)
            db.add(me)
        db.flush()
    elif char is not None and body.character is None:
        me.character_name = char
    if body.character is not None:
        charaktere.setzen(db, me, body.character, beitritt=True)
    if neu:
        charaktere.neuzugang(db, me)
    if c.organization_id:
        ensure_org_member(db, c.organization_id, user)
    db.commit()
    db.refresh(c)
    return campaign_out(db, c, me)


@router.get("/campaigns/{campaignId}", response_model=schemas.CampaignOut, response_model_exclude_unset=True)
def get_campaign(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c, me = _load(db, campaignId, user)
    return campaign_out(db, c, me)


@router.patch("/campaigns/{campaignId}", response_model=schemas.CampaignOut, response_model_exclude_unset=True)
def patch_campaign(
    campaignId: str, body: schemas.CampaignPatch, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    c, me = _load(db, campaignId, user)
    require_gm(me)
    f = body.model_fields_set
    if "title" in f:
        title = (body.title or "").strip()
        if not title:
            raise errors.bad_request("validation_error", "validation_error.title")
        c.title = title
    if "description" in f:
        c.description = (body.description or "").strip()
    if "world_info" in f:
        c.world_info = (body.world_info or "").strip() or None
    if "language" in f and body.language is not None:
        c.language = body.language
    if "system" in f:
        c.system = body.system
    if "system_name" in f:
        c.system_name = (body.system_name or "").strip() or None
    if "allow_external_transcription" in f and body.allow_external_transcription is not None:
        c.allow_external_transcription = body.allow_external_transcription
    if "allow_cloud_summary" in f and body.allow_cloud_summary is not None:
        c.allow_cloud_summary = body.allow_cloud_summary
    if "archived" in f and body.archived is not None:
        if body.archived and c.archived_at is None:
            c.archived_at = utcnow()
            from app.routers.miteinander import offene_umfrage

            offen = offene_umfrage(db, c.id)
            if offen is not None:  # abgeschlossen: keine Terminabstimmung mehr
                offen.status = "cancelled"
        elif not body.archived:
            c.archived_at = None
    if "hotwords" in f and body.hotwords is not None:
        from app import namenshilfe

        namenshilfe.setzen(db, c, body.hotwords)
    if "cover_preset" in f:
        from app.bilder import cover_ordner, loeschen

        c.cover_preset = body.cover_preset
        c.cover_image_updated_at = None  # ein eigenes Bild wird damit ersetzt
        loeschen(cover_ordner(c.id))
    db.commit()
    db.refresh(c)
    return campaign_out(db, c, me)


@router.post("/campaigns/{campaignId}/invites", status_code=201, response_model=schemas.InviteOut)
def new_invite(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _, me = _load(db, campaignId, user)
    require_gm(me)
    inv = create_invite(db, campaignId, user)
    db.commit()
    return schemas.InviteOut(code=inv.code, expires_at=inv.expires_at)


@router.patch("/campaigns/{campaignId}/members/{memberId}", response_model=schemas.MemberOut,
              response_model_exclude_unset=True)
def patch_member(
    campaignId: str, memberId: str, body: schemas.MemberPatch,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _, me = _load(db, campaignId, user)
    target = db.get(Member, memberId)
    if target is None or target.campaign_id != campaignId or not target.aktiv:
        raise errors.not_found("member")
    f = body.model_fields_set
    selbst = target.id == me.id
    if ("character_summary" in f or "character_backstory" in f) and not selbst:
        raise errors.forbidden("forbidden.character_self")
    if "character_name" in f and not selbst and me.role != "gm":
        raise errors.forbidden("forbidden.own_character")
    if "role" in f and body.role is not None and body.role != target.role and me.role != "gm":
        raise errors.forbidden("forbidden.role")
    if "character_name" in f:
        target.character_name = (body.character_name or "").strip() or None
    if "character_summary" in f:
        target.character_summary = (body.character_summary or "").strip() or None
    if "character_backstory" in f:
        target.character_backstory = (body.character_backstory or "").strip() or None
    if "role" in f and body.role is not None and body.role != target.role:
        if target.role == "gm":
            if aktive_sl_anzahl(db, campaignId) <= 1:
                raise errors.conflict("last_gm")
        target.role = body.role
    db.commit()
    db.refresh(me)
    return member_out(target, me)


@router.delete("/campaigns/{campaignId}", status_code=204)
def delete_campaign(campaignId: str, body: schemas.DeleteCampaignRequest, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    """Kampagne endgültig löschen – für alle, mit allen Dateien (0.4.5)."""
    from app.konto import _kampagne_loeschen

    c, me = _load(db, campaignId, user)
    require_gm(me)
    if " ".join(body.confirm_title.split()).casefold() != " ".join(c.title.split()).casefold():
        raise errors.bad_request("confirmation_mismatch", "confirmation_mismatch.title")
    _kampagne_loeschen(db, c)
    db.commit()
    return Response(status_code=204)


@router.delete("/campaigns/{campaignId}/members/{memberId}", status_code=204)
def remove_member(campaignId: str, memberId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Eigene memberId = verlassen, fremde = entfernen (nur SL). Das Mitglied bleibt mit leftAt stehen (0.4.5)."""
    from app.models import DateVote, SessionSeen

    _, me = _load(db, campaignId, user)
    target = db.get(Member, memberId)
    if target is None or target.campaign_id != campaignId or not target.aktiv:
        raise errors.not_found("member")
    if target.id != me.id and me.role != "gm":
        raise errors.forbidden()
    if target.role == "gm" and aktive_sl_anzahl(db, campaignId) <= 1:
        raise errors.conflict("last_gm")
    set_recording_consent(db, target, False)  # Widerruf bleibt als Nachweis im Protokoll
    target.character_backstory = None
    target.left_at = utcnow()
    db.execute(SessionSeen.__table__.delete().where(SessionSeen.member_id == target.id))
    db.execute(DateVote.__table__.delete().where(DateVote.member_id == target.id))
    db.commit()
    return Response(status_code=204)


@router.put("/campaigns/{campaignId}/recording-consent", response_model=schemas.MemberOut,
            response_model_exclude_unset=True)
def recording_consent(
    campaignId: str, body: schemas.RecordingConsentIn, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _, me = _load(db, campaignId, user)
    set_recording_consent(db, me, body.granted)
    db.commit()
    return member_out(me, me)


@router.post("/campaigns/{campaignId}/seen", status_code=204)
def seen(campaignId: str, body: schemas.SeenIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _, me = _load(db, campaignId, user)
    if body.area == "chronicle":
        me.chronicle_seen_at = utcnow()
    else:
        me.bible_seen_at = utcnow()
    db.commit()
    return Response(status_code=204)


@router.get("/campaigns/{campaignId}/usage", response_model=schemas.UsageOut)
def usage(
    campaignId: str, month: str | None = Query(default=None),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    _, me = _load(db, campaignId, user)
    require_gm(me)
    if month is None:
        month = utcnow().strftime("%Y-%m")
    elif not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise errors.bad_request("validation_error", "validation_error.month")
    jahr, mon = int(month[:4]), int(month[5:])
    von = datetime(jahr, mon, 1, tzinfo=timezone.utc)
    bis = datetime(jahr + (mon == 12), mon % 12 + 1, 1, tzinfo=timezone.utc)
    zeilen = db.scalars(select(UsageLog).where(UsageLog.campaign_id == campaignId, UsageLog.created_at >= von,
                                               UsageLog.created_at < bis)).all()
    sessions = {z.session_id for z in zeilen if z.kind == "transcription" and z.session_id}
    documents = {z.document_id for z in zeilen if z.document_id}
    return schemas.UsageOut(
        month=month, sessions=len(sessions),
        audio_seconds=int(round(sum(z.audio_seconds for z in zeilen if z.kind == "transcription"))),
        documents=len(documents), cost_estimate_cents=sum(z.cost_cents for z in zeilen), billed_to="gm",
    )


# ---------- Bilder ----------
def _bild_antwort(pfad) -> Response:
    from app.bilder import MEDIENTYP

    return Response(pfad.read_bytes(), media_type=MEDIENTYP[pfad.suffix[1:]],
                    headers={"Cache-Control": "private, max-age=86400"})


@router.get("/campaigns/{campaignId}/cover-image")
def get_cover(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.bilder import cover_ordner, finden

    c, _ = _load(db, campaignId, user)
    pfad = finden(cover_ordner(c.id), "cover") if c.cover_image_updated_at else None
    if pfad is None:
        raise errors.not_found("image")
    return _bild_antwort(pfad)


@router.put("/campaigns/{campaignId}/cover-image", response_model=schemas.CampaignOut,
            response_model_exclude_unset=True)
async def put_cover(campaignId: str, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    from app.bilder import COVER_MAX_BYTES, cover_speichern

    c, me = _load(db, campaignId, user)
    require_gm(me)
    daten = await _koerper(request, COVER_MAX_BYTES)
    cover_speichern(c.id, daten, request.headers.get("content-type"))
    c.cover_preset, c.cover_image_updated_at = None, utcnow()
    db.commit()
    db.refresh(c)
    return campaign_out(db, c, me)


@router.delete("/campaigns/{campaignId}/cover-image", status_code=204)
def delete_cover(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.bilder import cover_ordner, loeschen

    c, me = _load(db, campaignId, user)
    require_gm(me)
    loeschen(cover_ordner(c.id))
    c.cover_image_updated_at = None
    db.commit()
    return Response(status_code=204)


def _mitglied(db: Session, campaign_id: str, member_id: str) -> Member:
    m = db.get(Member, member_id)
    if m is None or m.campaign_id != campaign_id:
        raise errors.not_found("member")
    return m


@router.get("/campaigns/{campaignId}/members/{memberId}/portrait")
def get_portrait(campaignId: str, memberId: str, size: str = Query("full", pattern="^(thumb|full)$"),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.bilder import finden, portrait_ordner

    _load(db, campaignId, user)
    m = _mitglied(db, campaignId, memberId)
    pfad = finden(portrait_ordner(m.id), size) if m.portrait_updated_at else None
    if pfad is None:
        raise errors.not_found("image")
    return _bild_antwort(pfad)


@router.put("/campaigns/{campaignId}/members/{memberId}/portrait", response_model=schemas.MemberOut,
            response_model_exclude_unset=True)
async def put_portrait(campaignId: str, memberId: str, request: Request,
                       crop_x: int | None = Query(None, alias="cropX", ge=0),
                       crop_y: int | None = Query(None, alias="cropY", ge=0),
                       crop_size: int | None = Query(None, alias="cropSize", ge=1),
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.bilder import PORTRAIT_MAX_BYTES, portrait_speichern

    _, me = _load(db, campaignId, user)
    m = _mitglied(db, campaignId, memberId)
    if m.id != me.id:
        raise errors.forbidden("forbidden.portrait")
    daten = await _koerper(request, PORTRAIT_MAX_BYTES)
    portrait_speichern(m.id, daten, request.headers.get("content-type"), crop_x, crop_y, crop_size)
    m.portrait_updated_at = utcnow()
    db.commit()
    db.refresh(m)
    return member_out(m, me)


@router.delete("/campaigns/{campaignId}/members/{memberId}/portrait", status_code=204)
def delete_portrait(campaignId: str, memberId: str, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    from app.bilder import loeschen, portrait_ordner

    _, me = _load(db, campaignId, user)
    m = _mitglied(db, campaignId, memberId)
    if m.id != me.id and me.role != "gm":  # die SL darf zur Moderation jedes Bild löschen
        raise errors.forbidden("forbidden.portrait")
    loeschen(portrait_ordner(m.id))
    m.portrait_updated_at = None
    db.commit()
    return Response(status_code=204)


async def _koerper(request: Request, max_bytes: int) -> bytes:
    """Rohdaten lesen, aber nie mehr als erlaubt in den Speicher holen."""
    laenge = request.headers.get("content-length")
    if laenge and laenge.isdigit() and int(laenge) > max_bytes:
        raise errors.ApiError(413, "image_too_large", mb=max_bytes // (1024 * 1024))
    teile, n = [], 0
    async for teil in request.stream():
        n += len(teil)
        if n > max_bytes:
            raise errors.ApiError(413, "image_too_large", mb=max_bytes // (1024 * 1024))
        teile.append(teil)
    return b"".join(teile)
