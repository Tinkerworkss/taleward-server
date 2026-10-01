"""Charaktere (Schnittstelle 0.4.7): eigener Charakter, mitgebrachte Welt, Chronik, Prüfliste und Hinweise der SL.
Logik in app/charaktere.py."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import charaktere, errors, schemas
from app.access import current_user, require_member
from app.db import get_db
from app.models import GmNotice, Member, User
from app.routers.pruefung import proposal_out
from app.services import member_out

router = APIRouter(tags=["Charaktere"])


@router.put("/campaigns/{campaignId}/members/me/character", response_model=schemas.MemberOut,
            response_model_exclude_unset=True)
def put_character(campaignId: str, body: schemas.CharacterIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    charaktere.setzen(db, me, body)
    db.commit()
    db.refresh(me)
    return member_out(me, me)


@router.delete("/campaigns/{campaignId}/members/me/character", status_code=204)
def delete_character(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    charaktere.loesen(db, me)
    db.commit()
    return Response(status_code=204)


@router.get("/campaigns/{campaignId}/members/me/world", response_model=schemas.WorldOut)
def get_world(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    return schemas.WorldOut(entries=charaktere.stand(db, me))


@router.post("/campaigns/{campaignId}/members/me/world", response_model=schemas.WorldOut)
def post_world(campaignId: str, body: schemas.WorldIn, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    aus = charaktere.einreichen(db, me, body.entries)
    db.commit()
    return schemas.WorldOut(entries=aus)


@router.get("/campaigns/{campaignId}/members/me/chronicle")
def get_chronicle(campaignId: str, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    """Auch nach dem Verlassen, solange das Konto besteht – deshalb ohne die Prüfung auf aktive Mitgliedschaft."""
    me = db.scalar(select(Member).where(Member.campaign_id == campaignId, Member.user_id == user.id))
    if me is None:
        raise errors.not_found("campaign")
    return charaktere.chronik(db, me, request)


@router.get("/campaigns/{campaignId}/character-proposals", tags=["Vorschläge"],
            response_model=list[schemas.ProposalOut], response_model_exclude_unset=True)
def character_proposals(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    if me.role != "gm":
        raise errors.not_found("campaign")  # Spieler: nicht einmal die Existenz der Liste
    return [proposal_out(p) for p in charaktere.vorschlaege(db, campaignId)]


@router.delete("/campaigns/{campaignId}/gm-notices/{noticeId}", tags=["Kampagnen"], status_code=204)
def delete_notice(campaignId: str, noticeId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    me = require_member(db, campaignId, user)
    n = db.get(GmNotice, noticeId)
    if me.role != "gm" or n is None or n.campaign_id != campaignId:
        raise errors.not_found("notice")
    db.delete(n)
    db.commit()
    return Response(status_code=204)
