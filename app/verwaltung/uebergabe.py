"""Seite „Auftragsverarbeitung und Übergabe“: Was der Verein mit Dienstleistern vereinbaren muss und was eine neue
Verwaltung braucht. Zeigt nur, ob etwas hinterlegt ist – nie Schlüssel oder Passwörter selbst.

server_meta: av.<anbieter> = Datum (JJJJ-MM-TT), an dem die Verwaltung den Vertrag als abgeschlossen vermerkt hat.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.einstellungen import extern_konfig, llm_konfig, meta_lesen, meta_schreiben
from app.models import OrgMember, User
from app.verwaltung.router import _seite, _zurueck, csrf_pruefen, verwalter

router = APIRouter(prefix="/verwaltung", include_in_schema=False)

ANBIETER = {
    "hosting": {"name": "Hosting (z. B. Hostinger)",
                "link": "https://www.hostinger.com/legal/dpa",
                "hilfe": "Bei Hostinger ist der Vertrag (Data Processing Addendum) Teil der Nutzungsbedingungen und gilt "
                         "mit der Bestellung. Beim Bestellen ein Rechenzentrum in der EU wählen; die Liste der "
                         "Unterauftragsverarbeiter im Vertrag ansehen. Als Datum das der Bestellung eintragen. Bei einem "
                         "Server im Verein oder zu Hause entfällt das."},
    "mistral": {"name": "Mistral AI (Cloud-Transkription, Cloud-Sprachmodell)",
                "link": "https://legal.mistral.ai/terms/data-processing-addendum/",
                "hilfe": "Das Data Processing Addendum ist Teil der Nutzungsbedingungen von Mistral. Prüfen, dass im "
                         "Konto keine Nutzung der Daten zum Training erlaubt ist."},
}


def _mistral_genutzt(db: Session) -> bool:
    k = llm_konfig(db)
    return extern_konfig(db).anbieter == "mistral" or (k.art == "api" and k.ist_mistral)


@router.get("/uebergabe", response_class=HTMLResponse)
def uebergabe(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app import benachrichtigung, sicherung
    from app.config import get_settings

    admins = db.scalars(select(User).join(OrgMember, OrgMember.user_id == User.id)
                        .where(OrgMember.role == "admin").order_by(User.username)).unique().all()
    k = llm_konfig(db)
    melden = benachrichtigung.konfig(db)
    hinterlegt = [
        ("Schlüssel für Mistral (Transkription)", bool(extern_konfig(db).api_key)),
        ("Schlüssel für das Cloud-Sprachmodell", k.art == "api" and bool(k.api_key)),
        ("Hugging-Face-Zugang", bool(meta_lesen(db, "hf.token") or get_settings().hf_token)),
        ("Benachrichtigungen per ntfy", melden.ntfy),
        ("Benachrichtigungen per E-Mail", melden.email),
    ]
    anbieter = [{"schluessel": s, **a, "datum": meta_lesen(db, f"av.{s}") or "",
                 "noetig": s == "hosting" or _mistral_genutzt(db)} for s, a in ANBIETER.items()]
    e = sicherung.einstellungen(db)
    return _seite(request, "uebergabe.html", user, db, admins=admins, hinterlegt=hinterlegt, anbieter=anbieter,
                  sicherung_ordner=e["ordner"], ablage=str(sicherung.ablage()), heute=date.today().isoformat())


@router.post("/uebergabe/av", dependencies=[Depends(csrf_pruefen)])
async def av_speichern(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    form = await request.form()
    for s in ANBIETER:
        wert = str(form.get(f"av_{s}", "")).strip()
        try:
            wert = date.fromisoformat(wert).isoformat() if wert else ""
        except ValueError:
            wert = ""
        meta_schreiben(db, f"av.{s}", wert)
    db.commit()
    return _zurueck("/uebergabe", "gespeichert")


