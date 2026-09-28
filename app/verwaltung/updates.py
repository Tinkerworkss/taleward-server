"""Seite „Updates“: neue Fassungen von Server, App und Worker; Freigabe automatisch oder von Hand."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import aktualisierung
from app.db import get_db
from app.einstellungen import angaben, meta_lesen
from app.models import User
from app.verwaltung.router import _seite, _zurueck, csrf_pruefen, verwalter

router = APIRouter(prefix="/verwaltung", include_in_schema=False)


@router.get("/updates", response_class=HTMLResponse)
def seite(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app.config import get_settings

    a = angaben(db)
    return _seite(request, "updates.html", user, db,
                  eigene=aktualisierung.eigene_fassung(), server_neu=aktualisierung.server_update(db),
                  server_stand=aktualisierung.stand(db, "server"), docker=bool(os.environ.get("TALEWARD_DOCKER")),
                  modus=aktualisierung.modus(db), zeilen=aktualisierung.uebersicht(db),
                  geprueft=_zeitpunkt(meta_lesen(db, "update.geprueft")), fehler_pruefung=meta_lesen(db, "update.fehler"),
                  pruefung_an=get_settings().update_check,
                  app_von_hand=bool(a.app_latest_version or a.app_download_url))


def _zeitpunkt(wert: str | None):
    from datetime import datetime, timezone

    if not wert:
        return None
    z = datetime.fromisoformat(wert)
    return z if z.tzinfo else z.replace(tzinfo=timezone.utc)


@router.post("/updates/pruefen", dependencies=[Depends(csrf_pruefen)])
def pruefen(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    aktualisierung.pruefen(db)
    return _zurueck("/updates", "updates_geprueft")


@router.post("/updates/modus", dependencies=[Depends(csrf_pruefen)])
async def modus(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    form = await request.form()
    aktualisierung.modus_setzen(db, str(form.get("modus", "automatisch")))
    return _zurueck("/updates", "gespeichert")


@router.post("/updates/freigeben", dependencies=[Depends(csrf_pruefen)])
async def freigeben(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    form = await request.form()
    art, version = str(form.get("art", "")), str(form.get("version", ""))
    if art not in aktualisierung.ARTEN or not aktualisierung.freigeben(db, art, version):
        return _zurueck("/updates")
    return _zurueck("/updates", "freigegeben")
