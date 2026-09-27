"""Verwaltung → Anmeldung: Anmeldedienste (Google, Discord, Apple, Microsoft) und E-Mail für „Passwort vergessen“.

Jeder Server meldet sich selbst bei den Diensten an. Schlüssel sind nur schreibbar (Anzeige: „hinterlegt“).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import anmeldedienste, mail
from app.db import get_db
from app.einstellungen import meta_schreiben, oeffentliche_adresse
from app.models import User
from app.verwaltung.router import _seite, _zurueck, csrf_pruefen, tr, verwalter

router = APIRouter(prefix="/verwaltung", include_in_schema=False)

ANLEITUNG = {
    "google": ("https://console.cloud.google.com/apis/credentials", [
        "In der Google Cloud Console ein Projekt anlegen (z. B. „Taleward Verein“).",
        "„OAuth-Zustimmungsbildschirm“: Nutzertyp Extern, App-Name und Kontakt eintragen, Bereiche openid, email, profile.",
        "„Anmeldedaten“ → „Anmeldedaten erstellen“ → „OAuth-Client-ID“ → Typ Webanwendung.",
        "Bei „Autorisierte Weiterleitungs-URIs“ die Rückleitungsadresse unten eintragen.",
        "Client-ID und Clientschlüssel hier eintragen.",
    ]),
    "discord": ("https://discord.com/developers/applications", [
        "Im Discord Developer Portal „New Application“ anlegen (z. B. „Taleward Verein“).",
        "Unter „OAuth2“ bei „Redirects“ die Rückleitungsadresse unten eintragen.",
        "Client ID und Client Secret („Reset Secret“) hier eintragen.",
    ]),
    "microsoft": ("https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade", [
        "Im Microsoft Entra Admin Center „App-Registrierungen“ → „Neue Registrierung“.",
        "Kontotypen: „Konten in allen Organisationsverzeichnissen und persönliche Microsoft-Konten“.",
        "Umleitungs-URI: Plattform Web, die Rückleitungsadresse unten.",
        "Unter „Zertifikate & Geheimnisse“ einen geheimen Clientschlüssel erstellen.",
        "Anwendungs-ID (Client) und den Wert des Geheimnisses hier eintragen.",
    ]),
    "apple": ("https://developer.apple.com/account/resources/identifiers/list/serviceId", [
        "Braucht ein Apple-Entwicklerkonto (kostenpflichtig). Nötig erst mit einer iPhone-Fassung der App.",
        "Unter „Identifiers“ eine Services ID anlegen, „Sign in with Apple“ aktivieren, Domain und Rückleitungsadresse unten eintragen.",
        "Unter „Keys“ einen Schlüssel mit „Sign in with Apple“ erzeugen und die .p8-Datei laden.",
        "Services ID (als Client-ID), Team-ID, Key-ID und den Inhalt der .p8-Datei hier eintragen.",
    ]),
}


def _dienste(db: Session, basis: str) -> list[dict]:
    return [{"id": d, "name": anmeldedienste.DIENSTE[d]["name"], "k": anmeldedienste.konfig(db, d),
             "rueck": anmeldedienste.rueckleitung(basis, d), "link": ANLEITUNG[d][0], "schritte": ANLEITUNG[d][1]}
            for d in anmeldedienste.REIHENFOLGE]


@router.get("/anmeldung", response_class=HTMLResponse)
def anmeldung(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db), fehler: str = ""):
    basis = oeffentliche_adresse(db, request)
    return _seite(request, "anmeldung.html", user, db, dienste=_dienste(db, basis), basis=basis,
                  https=basis.startswith("https://"), mail_bereit=mail.kann_senden(db), fehler=fehler)


@router.post("/anmeldung/{dienst}", dependencies=[Depends(csrf_pruefen)])
async def anmeldung_speichern(dienst: str, request: Request, user: User = Depends(verwalter),
                              db: Session = Depends(get_db)):
    _ = tr(request)
    if dienst not in anmeldedienste.DIENSTE:
        return _zurueck("/anmeldung")
    form = await request.form()
    wert = {k: str(form.get(k, "")).strip() for k in ("client_id", "client_secret", "team_id", "key_id",
                                                        "private_key", "entfernen")}
    if wert["entfernen"]:
        for k in ("client_id", "client_secret", "team_id", "key_id", "private_key"):
            meta_schreiben(db, f"oidc.{dienst}.{k}", "")
        db.commit()
        return _zurueck(f"/anmeldung#{dienst}", "gespeichert")
    if dienst == "apple" and wert["private_key"] and "PRIVATE KEY" not in wert["private_key"]:
        antwort = anmeldung(request, user, db, fehler=_("Bitte den ganzen Inhalt der .p8-Datei einfügen (mit BEGIN/END PRIVATE KEY)."))
        antwort.status_code = 400
        return antwort
    for k in ("client_id", "team_id", "key_id"):
        if wert[k] or k == "client_id":
            meta_schreiben(db, f"oidc.{dienst}.{k}", wert[k][:300])
    for k in ("client_secret", "private_key"):  # nur schreibbar: leer = behalten
        if wert[k]:
            meta_schreiben(db, f"oidc.{dienst}.{k}", wert[k][:5000])
    db.commit()
    return _zurueck(f"/anmeldung#{dienst}", "gespeichert")
