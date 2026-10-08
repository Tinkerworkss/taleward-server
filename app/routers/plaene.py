"""Kapitelpläne der SL (Schnittstelle 0.4.12).

Nur die SL der Kampagne sieht und ändert Pläne. Für alle anderen gibt es keinen Weg dorthin – auch nicht die Anzahl –,
darum 404 statt 403. Pläne fließen nie in Kapitel, Vorschläge, Gegenprüfung oder Probelauf; nur die Namen gehen als
Schreibhilfe in die Transkription des Kapitels mit derselben Nummer (app/namenshilfe.py).

Verknüpfte Einträge und Unterlagen müssen zur Kampagne gehören (sonst 400 invalid_input). Werden sie später gelöscht,
fallen sie beim Lesen aus dem Plan heraus – der Plan selbst wird dafür nicht angefasst.
"""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, schemas
from app.access import current_user, membership
from app.db import get_db, utcnow
from app.models import CampaignDocument, ChapterPlan, Entry, User

router = APIRouter(tags=["Kapitelplan"])


def _sl(db: Session, campaign_id: str, user: User) -> None:
    me = membership(db, campaign_id, user)
    if me is None or me.role != "gm":
        raise errors.not_found("plan")


def _plan(db: Session, plan_id: str, user: User) -> ChapterPlan:
    p = db.get(ChapterPlan, plan_id)
    if p is None:
        raise errors.not_found("plan")
    _sl(db, p.campaign_id, user)
    return p


def _vorhandene(db: Session, campaign_id: str) -> tuple[set[str], set[str]]:
    eintraege = set(db.scalars(select(Entry.id).where(Entry.campaign_id == campaign_id)))
    unterlagen = set(db.scalars(select(CampaignDocument.id).where(CampaignDocument.campaign_id == campaign_id)))
    return eintraege, unterlagen


def plan_out(db: Session, p: ChapterPlan, vorhanden: tuple[set[str], set[str]] | None = None) -> schemas.ChapterPlanOut:
    eintraege, unterlagen = vorhanden or _vorhandene(db, p.campaign_id)
    szenen = []
    for sz in json.loads(p.scenes or "[]"):
        sz["entryIds"] = [i for i in sz.get("entryIds") or [] if i in eintraege]
        szenen.append(schemas.PlanScene.model_validate(sz))
    return schemas.ChapterPlanOut(
        id=p.id, campaign_id=p.campaign_id, title=p.title, session_number=p.session_number, state=p.state,
        notes=p.notes, scenes=szenen, names=json.loads(p.names or "[]"),
        document_ids=[i for i in json.loads(p.document_ids or "[]") if i in unterlagen],
        created_at=p.created_at, updated_at=p.updated_at)


def _eindeutig(werte: list[str]) -> list[str]:
    gesehen, aus = set(), []
    for w in werte:
        if w not in gesehen:
            gesehen.add(w)
            aus.append(w)
    return aus


def _setzen(db: Session, p: ChapterPlan, felder: dict) -> None:
    """Felder übernehmen und Verweise prüfen. felder: nur, was gesetzt werden soll (camelCase-frei, Modellwerte)."""
    eintraege, unterlagen = _vorhandene(db, p.campaign_id)
    if "title" in felder:
        titel = felder["title"].strip()
        if not titel:
            raise errors.bad_request("validation_error", "validation_error.empty", field="title")
        p.title = titel
    if "session_number" in felder:
        p.session_number = felder["session_number"]
    if "state" in felder:
        p.state = felder["state"]
    if "notes" in felder:
        p.notes = (felder["notes"] or "").strip() or None
    if "scenes" in felder:
        szenen = felder["scenes"]
        ids = [s.id.lower() for s in szenen]
        if len(ids) != len(set(ids)):
            raise errors.bad_request("invalid_input", "invalid_input.plan_scene")
        aus = []
        for s in szenen:
            if any(i not in eintraege for i in s.entry_ids):
                raise errors.bad_request("invalid_input", "invalid_input.plan_link")
            titel = s.title.strip()
            if not titel:
                raise errors.bad_request("validation_error", "validation_error.empty", field="title")
            aus.append({"id": s.id.lower(), "title": titel, "notes": (s.notes or "").strip() or None,
                        "entryIds": _eindeutig(s.entry_ids), "state": s.state})
        p.scenes = json.dumps(aus, ensure_ascii=False)
    if "names" in felder:
        namen = _eindeutig([n.strip() for n in felder["names"] if n.strip()])
        p.names = json.dumps(namen, ensure_ascii=False)
    if "document_ids" in felder:
        if any(i not in unterlagen for i in felder["document_ids"]):
            raise errors.bad_request("invalid_input", "invalid_input.plan_link")
        p.document_ids = json.dumps(_eindeutig(felder["document_ids"]))
    p.updated_at = utcnow()


def _reihenfolge(p: ChapterPlan) -> tuple:
    return (p.session_number is None, p.session_number or 0, p.created_at)


@router.get("/campaigns/{campaignId}/plans", response_model=list[schemas.ChapterPlanOut])
def plans_list(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _sl(db, campaignId, user)
    vorhanden = _vorhandene(db, campaignId)
    plaene = sorted(db.scalars(select(ChapterPlan).where(ChapterPlan.campaign_id == campaignId)), key=_reihenfolge)
    return [plan_out(db, p, vorhanden) for p in plaene]


@router.post("/campaigns/{campaignId}/plans", status_code=201, response_model=schemas.ChapterPlanOut)
def plan_create(campaignId: str, body: schemas.ChapterPlanIn, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    _sl(db, campaignId, user)
    jetzt = utcnow()
    p = ChapterPlan(campaign_id=campaignId, created_at=jetzt, updated_at=jetzt)
    _setzen(db, p, {f: getattr(body, f) for f in ("title", "session_number", "state", "notes", "scenes", "names",
                                                    "document_ids")})
    db.add(p)
    db.commit()
    db.refresh(p)
    return plan_out(db, p)


@router.get("/plans/{planId}", response_model=schemas.ChapterPlanOut)
def plan_get(planId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return plan_out(db, _plan(db, planId, user))


@router.patch("/plans/{planId}", response_model=schemas.ChapterPlanOut)
def plan_patch(planId: str, body: schemas.ChapterPlanPatchIn, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    p = _plan(db, planId, user)
    gesetzt = body.model_fields_set
    if "if_updated_at" in gesetzt and body.if_updated_at is not None:
        erwartet = schemas._as_utc(body.if_updated_at)
        if abs(schemas._as_utc(p.updated_at) - erwartet) > timedelta(milliseconds=1):
            raise errors.conflict("conflict", "conflict.plan")
    felder = {}
    for f in ("title", "session_number", "state", "notes", "scenes", "names", "document_ids"):
        if f not in gesetzt:
            continue
        wert = getattr(body, f)
        if wert is None and f in ("title", "state", "scenes", "names", "document_ids"):
            raise errors.bad_request("validation_error", "validation_error.value", field=f)
        felder[f] = wert
    _setzen(db, p, felder)
    db.commit()
    db.refresh(p)
    return plan_out(db, p)


@router.delete("/plans/{planId}", status_code=204)
def plan_delete(planId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_plan(db, planId, user))
    db.commit()
    return Response(status_code=204)


def gespielt_markieren(db: Session, campaign_id: str, nummer: int) -> None:
    """Session mit dieser Nummer veröffentlicht: Pläne für dieses Kapitel gelten als gespielt (die SL kann es
    zurücksetzen)."""
    for p in db.scalars(select(ChapterPlan).where(ChapterPlan.campaign_id == campaign_id,
                                                  ChapterPlan.session_number == nummer)):
        if p.state != "played":
            p.state, p.updated_at = "played", utcnow()


def namen_fuer(db: Session, campaign_id: str, nummer: int | None) -> list[str]:
    """Namen aus den Plänen für das Kapitel mit dieser Nummer – nur die Wörter, für die Schreibhilfe."""
    if nummer is None:
        return []
    aus: list[str] = []
    for p in db.scalars(select(ChapterPlan).where(ChapterPlan.campaign_id == campaign_id,
                                                  ChapterPlan.session_number == nummer)
                        .order_by(ChapterPlan.created_at)):
        aus += [n for n in json.loads(p.names or "[]") if isinstance(n, str)]
    return _eindeutig(aus)
