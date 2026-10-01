"""Kampagnenbibel. gm_only-Einträge erreichen Spieler nie – weder in Listen noch in der Suche."""
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import errors, schemas
from app.access import current_user, load_entry, require_gm, require_member
from app.db import get_db
from app.models import Entry, EntryMention, User
from app.services import apply_entry_input, entry_out, search_matches

router = APIRouter(tags=["Bibel"])


@router.get("/campaigns/{campaignId}/entries", response_model=list[schemas.EntryOut], response_model_exclude_unset=True)
def list_entries(
    campaignId: str,
    type: schemas.EntryType | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    me = require_member(db, campaignId, user)
    is_gm = me.role == "gm"
    stmt = (
        select(Entry).where(Entry.campaign_id == campaignId)
        .options(selectinload(Entry.mentions).selectinload(EntryMention.session))
    )
    if not is_gm:
        stmt = stmt.where(Entry.visibility == "public")
    if type is not None:
        stmt = stmt.where(Entry.type == type)
    entries = list(db.scalars(stmt))
    if not is_gm:
        entries = [e for e in entries if me.id not in e.hidden_member_ids]
    if q and q.strip():
        entries = [e for e in entries if search_matches(e, q, is_gm)]
    entries.sort(key=lambda e: e.name.casefold())
    return [entry_out(e, is_gm, me.id) for e in entries]


@router.post("/campaigns/{campaignId}/entries", status_code=201, response_model=schemas.EntryOut, response_model_exclude_unset=True)
def create_entry(
    campaignId: str, body: schemas.EntryInput, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    me = require_member(db, campaignId, user)
    require_gm(me)
    if body.type is None or not (body.name or "").strip():
        raise errors.bad_request("validation_error", "validation_error.type_name")
    if body.type == "pc":
        raise errors.bad_request("validation_error", "validation_error.pc")
    # Ohne Angabe ist ein Eintrag geheim – lieber versehentlich zu wenig verraten als zu viel.
    e = Entry(campaign_id=campaignId, type="", name="", summary="", visibility="gm_only")
    db.add(e)
    db.flush()  # ID für die Verborgen-Liste
    apply_entry_input(db, e, body)
    db.commit()
    db.refresh(e)
    return entry_out(e, True)


@router.get("/entries/{entryId}", response_model=schemas.EntryOut, response_model_exclude_unset=True)
def get_entry(entryId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_entry(db, entryId, user)
    return entry_out(acc.entry, acc.is_gm, acc.member.id)


@router.patch("/entries/{entryId}", response_model=schemas.EntryOut, response_model_exclude_unset=True)
def patch_entry(
    entryId: str, body: schemas.EntryInput, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    acc = load_entry(db, entryId, user)
    require_gm(acc.member)
    apply_entry_input(db, acc.entry, body, require_public_text=True)
    db.commit()
    db.refresh(acc.entry)
    return entry_out(acc.entry, True)


@router.delete("/entries/{entryId}", status_code=204)
def delete_entry(entryId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_entry(db, entryId, user)
    require_gm(acc.member)
    db.delete(acc.entry)
    db.commit()
    return Response(status_code=204)
