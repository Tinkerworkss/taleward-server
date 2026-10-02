"""Weboberfläche für den Betreiber unter /verwaltung.

Anmeldung: Konto mit Verwalter-Recht in einer Organisation (chronik create-user --admin bzw. chronik admin -u …).
Sitzung als eigenes Cookie (HttpOnly, SameSite=Strict, 12 h), unabhängig vom Token der App. Jedes Formular trägt
ein CSRF-Merkmal, das aus dem Cookie abgeleitet ist. Nach 5 Fehlversuchen in 5 Minuten wird die Anmeldung kurz gesperrt.

Die Verwaltung sieht keine Inhalte der Kampagnen – nur Titel, Nummern und Zustände, die für den Betrieb nötig sind.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import jwt
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors, queue
from app.config import get_settings
from app.db import get_db, server_id, utcnow
from app.einstellungen import angaben, meta_lesen, meta_schreiben, speichern
from app.models import (
    CampaignDocument,
    AuthMethod, Campaign, GameSession, Job, Member, Organization, OrgMember, User, Worker,
)
from app.security import verify_password
from app.verwaltung.i18n import SPRACHEN, sprache_von, uebersetzer
from app.verwaltung.lokaler_worker import KNECHT, META_KEY, ki_verfuegbar

HIER = Path(__file__).parent
COOKIE = "tw_verwaltung"
SITZUNG_STUNDEN = 12
MIN_PASSWORT = 8

router = APIRouter(prefix="/verwaltung", include_in_schema=False)
templates = Jinja2Templates(directory=str(HIER / "templates"))
BERLIN = ZoneInfo("Europe/Berlin")


def _zeitformat(sprache: str):
    def zeit(dt, mit_datum: bool = True) -> str:
        if dt is None:
            return "–"
        lokal = dt.astimezone(BERLIN)
        if sprache == "en":
            return lokal.strftime("%b %d, %Y, %H:%M" if mit_datum else "%H:%M")
        return lokal.strftime("%d.%m.%Y %H:%M" if mit_datum else "%H:%M")
    return zeit


def _groesse(n: int | None) -> str:
    if not n:
        return "0 MB"
    return f"{n / 1024 ** 3:.1f} GB" if n > 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def tr(request: Request):
    """Übersetzungsfunktion für die Sprache dieser Anfrage (Cookie, sonst Accept-Language, sonst Deutsch)."""
    return uebersetzer(sprache_von(request))


# Meldungen nach einer Aktion (?ok=…) – der Text ist zugleich der Übersetzungsschlüssel
MELDUNGEN = {
    "konto_angelegt": "Konto angelegt.",
    "passwort": "Passwort geändert. Das Konto ist auf allen Geräten abgemeldet.",
    "verwalter": "Verwalter-Recht geändert.",
    "konto_geloescht": "Konto gelöscht. Kommentare der Person bleiben als „gelöschtes Konto“ stehen.",
    "gespeichert": "Gespeichert.",
    "worker_einstellungen": "Gespeichert. Der Worker startet nach dem laufenden Auftrag mit den neuen Einstellungen neu.",
    "worker_neustart": "Der Worker startet neu, sobald er keinen Auftrag bearbeitet (spätestens in einer Minute).",
    "pausiert": "Worker pausiert – er nimmt keine neuen Aufträge an.",
    "fortgesetzt": "Worker nimmt wieder Aufträge an.",
    "gesperrt": "Zugangsschlüssel gesperrt.",
    "gestartet": "Lokaler Worker gestartet.",
    "beendet": "Lokaler Worker beendet.",
    "neustart": "Verarbeitung neu eingereiht.",
    "abgemeldet": "Abgemeldet.",
    "eingerichtet": "Eingerichtet. Das Konto „admin“ wurde gelöscht – ab jetzt meldest du dich mit deinem eigenen Konto an.",
    "extern": "Einstellungen zur externen Transkription gespeichert.",
    "sprachmodell": "Einstellungen zur Zusammenfassung gespeichert.",
    "gesichert": "Sicherung angelegt.",
    "modell": "Das Sprechermodell liegt jetzt auf dem Server.",
    "melden": "Benachrichtigungen gespeichert.",
    "melden_test": "Testnachricht verschickt.",
    "updates_geprueft": "Nach Updates gesucht.",
    "server_jetzt": "Update angefordert – der Server aktualisiert sich in den nächsten Minuten und ist dabei kurz nicht erreichbar.",
    "freigegeben": "Fassung freigegeben – App und Worker bekommen sie jetzt angeboten.",
    "aufbewahrung": "Gespeichert. Bitte den Datenschutzhinweis an die neue Aufbewahrung der Aufnahmen anpassen.",
    "aufbewahrung_zustimmung": "Gespeichert. Alle Mitglieder werden in der App gebeten, der Aufnahme mit dem neuen Wortlaut erneut zuzustimmen. Bitte auch den Datenschutzhinweis anpassen.",
}


# ---------------------------------------------------------------- Anmeldung und Schutz
class NichtAngemeldet(Exception):
    pass


SPERRE_VERSUCHE, SPERRE_SEKUNDEN = 5, 300   # je Name (über alle Adressen)
SPERRE_ADRESSE = 20                          # je Adresse (über alle Namen)


def _gesperrt(adresse: str, name: str) -> bool:
    from app.begrenzung import ZAEHLER

    return (ZAEHLER.voll(f"verw-name:{name}", SPERRE_VERSUCHE, SPERRE_SEKUNDEN)
            or ZAEHLER.voll(f"verw-adr:{adresse}", SPERRE_ADRESSE, SPERRE_SEKUNDEN))


def _fehlversuch(adresse: str, name: str) -> None:
    from app.begrenzung import ZAEHLER

    ZAEHLER.zaehlen(f"verw-name:{name}", SPERRE_SEKUNDEN)
    ZAEHLER.zaehlen(f"verw-adr:{adresse}", SPERRE_SEKUNDEN)


def _ist_verwalter(db: Session, user: User) -> bool:
    return db.scalar(select(func.count()).select_from(OrgMember).where(
        OrgMember.user_id == user.id, OrgMember.role == "admin")) > 0


def _aud(db: Session) -> str:
    return f"{server_id()}:verwaltung"


def _cookie_wert(db: Session, user: User) -> str:
    jetzt = utcnow()
    return jwt.encode({"sub": user.id, "tv": user.token_version, "aud": _aud(db), "iat": jetzt,
                       "exp": jetzt + timedelta(hours=SITZUNG_STUNDEN), "jti": secrets.token_hex(8)},
                      get_settings().jwt_secret, algorithm="HS256")


def _zu_leicht(passwort: str, *namen: str) -> bool:
    from app.konto import passwort_zu_schwach

    return passwort_zu_schwach(passwort, *namen)


def _csrf(cookie: str) -> str:
    return hmac.new(get_settings().jwt_secret.encode(), b"csrf:" + cookie.encode(), hashlib.sha256).hexdigest()[:40]


class EinrichtungNoetig(Exception):
    """Angemeldet mit dem Einrichtungskonto admin/admin – erst einen eigenen Verwalter anlegen."""


def sitzung(request: Request, db: Session = Depends(get_db)) -> User:
    """Gültige Verwaltungs-Sitzung (auch das Einrichtungskonto)."""
    wert = request.cookies.get(COOKIE)
    if not wert:
        raise NichtAngemeldet()
    try:
        daten = jwt.decode(wert, get_settings().jwt_secret, algorithms=["HS256"], audience=_aud(db))
    except jwt.PyJWTError:
        raise NichtAngemeldet() from None
    user = db.get(User, daten.get("sub"))
    if user is None or user.token_version != daten.get("tv") or not _ist_verwalter(db, user):
        raise NichtAngemeldet()
    abgemeldet = meta_lesen(db, f"verwaltung.abgemeldet.{user.id}")
    if abgemeldet and abgemeldet.isdigit() and int(daten.get("iat") or 0) < int(abgemeldet):
        raise NichtAngemeldet()  # nach dem Abmelden gilt keine ältere Sitzung mehr (auch nicht auf anderen Geräten)
    request.state.csrf = _csrf(wert)
    return user


def verwalter(request: Request, db: Session = Depends(get_db)) -> User:
    user = sitzung(request, db)
    if user.setup_account:
        raise EinrichtungNoetig()
    return user


def csrf_pruefen(request: Request, csrf: str = Form("")) -> None:
    wert = request.cookies.get(COOKIE, "")
    if not wert or not hmac.compare_digest(csrf, _csrf(wert)):
        raise errors.forbidden()


def _seite(request: Request, name: str, user: User | None, db: Session, **ctx) -> HTMLResponse:
    sprache = sprache_von(request)
    _ = uebersetzer(sprache)
    ok = request.query_params.get("ok")
    meldung = _(MELDUNGEN[ok]) if ok in MELDUNGEN else None
    return templates.TemplateResponse(request, name, {
        "user": user, "angaben": angaben(db), "csrf": getattr(request.state, "csrf", ""), "meldung": meldung,
        "aktiv": name.removesuffix(".html"), "_": _, "sprache": sprache, "sprachen": SPRACHEN,
        "zeit": _zeitformat(sprache), "groesse": _groesse, "leitsatz": None, **ctx,
    })


def _zurueck(pfad: str, ok: str | None = None) -> RedirectResponse:
    pfad, _, anker = pfad.partition("#")
    return RedirectResponse(f"/verwaltung{pfad}" + (f"?ok={ok}" if ok else "") + (f"#{anker}" if anker else ""),
                            status_code=303)


@router.get("/anmelden", response_class=HTMLResponse)
def anmelden_seite(request: Request, db: Session = Depends(get_db)):
    from app.einrichtung import braucht_einrichtung

    ersteinrichtung = db.scalar(select(func.count()).select_from(User).where(User.setup_account.is_(True))) > 0
    if braucht_einrichtung(db) and not ersteinrichtung:
        return RedirectResponse("/verwaltung/einrichtung", status_code=303)
    return _seite(request, "anmelden.html", None, db, kein_verwalter=False, fehler=None,
                  ersteinrichtung=ersteinrichtung)


@router.post("/anmelden")
def anmelden(request: Request, username: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    name = username.strip().lower()
    adresse = request.client.host if request.client else "?"
    fehler = None
    if _gesperrt(adresse, name):
        fehler = tr(request)("Zu viele Fehlversuche. Bitte in ein paar Minuten erneut versuchen.")
    else:
        user = db.scalar(select(User).where(User.username == name))
        methode = user and db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id,
                                                             AuthMethod.kind == "password"))
        if not (methode and verify_password(password, methode.secret)):
            _fehlversuch(adresse, name)
            fehler = tr(request)("Benutzername oder Passwort stimmt nicht.")
        elif not _ist_verwalter(db, user):
            fehler = tr(request)("Dieses Konto hat kein Verwalter-Recht. Freischalten mit: uv run chronik admin -u {name}",
                                 name=name)
        else:
            from app.begrenzung import ZAEHLER

            ZAEHLER.loeschen(f"verw-name:{name}")
            antwort = _zurueck("/einrichtung" if user.setup_account else "/")
            antwort.set_cookie(COOKIE, _cookie_wert(db, user), max_age=SITZUNG_STUNDEN * 3600, httponly=True,
                               samesite="strict", secure=request.url.scheme == "https", path="/verwaltung")
            return antwort
    antwort = _seite(request, "anmelden.html", None, db, kein_verwalter=False, fehler=fehler, ersteinrichtung=False)
    antwort.status_code = 400
    return antwort


@router.post("/abmelden", dependencies=[Depends(csrf_pruefen)])
def abmelden(user: User = Depends(sitzung), db: Session = Depends(get_db)):
    meta_schreiben(db, f"verwaltung.abgemeldet.{user.id}", str(int(time.time())))
    db.commit()
    antwort = RedirectResponse("/verwaltung/anmelden?ok=abgemeldet", status_code=303)
    antwort.delete_cookie(COOKIE, path="/verwaltung")
    return antwort


# ---------------------------------------------------------------- Übersicht
def _ordnergroesse(pfad: Path) -> int:
    if not pfad.exists():
        return 0
    return sum(f.stat().st_size for f in pfad.rglob("*") if f.is_file())


def _online_grenze():
    return utcnow() - timedelta(seconds=get_settings().worker_offline_after_seconds)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def uebersicht(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db), fehler: str = ""):
    from app.routers.auth import API_VERSION

    s = get_settings()
    zustaende = dict(db.execute(select(GameSession.state, func.count()).group_by(GameSession.state)).all())
    workers = db.scalars(select(Worker).where(Worker.revoked_at.is_(None))).all()
    grenze = _online_grenze()
    zahlen = {
        "konten": db.scalar(select(func.count()).select_from(User)),
        "kampagnen": db.scalar(select(func.count()).select_from(Campaign)),
        "veroeffentlicht": zustaende.get("published", 0),
        "wartend": zustaende.get("queued", 0),
        "in_arbeit": zustaende.get("transcribing", 0) + zustaende.get("summarizing", 0),
        "bei_sl": zustaende.get("awaiting_speakers", 0) + zustaende.get("awaiting_review", 0),
        "fehlgeschlagen": zustaende.get("failed", 0),
        "knechte_online": sum(1 for w in workers if w.last_seen_at and w.last_seen_at > grenze and not w.paused
                              and w.app_paused_since is None),
        "knechte": len(workers),
    }
    db_datei = s.data_dir / "chronik.db"
    speicher = {
        "datenbank": sum(p.stat().st_size for p in s.data_dir.glob("chronik.db*") if p.is_file()),
        "uploads": _ordnergroesse(s.data_dir / "uploads"),
        "hoerproben": _ordnergroesse(s.data_dir / "samples"),
    }
    from app import aufbewahrung, einrichtung, kosten, sicherung
    from app.einstellungen import meta_lesen

    punkte = [] if meta_lesen(db, "assistent.ausgeblendet") else einrichtung.stand(db)
    kosten_info = {"monat": kosten.monat_cent(db), "limit": kosten.limit_cent(db)}
    speicher["bilder"] = _ordnergroesse(s.data_dir / "bilder") + _ordnergroesse(s.data_dir / "unterlagen")
    return _seite(request, "uebersicht.html", user, db, zahlen=zahlen, speicher=speicher, api_version=API_VERSION,
                  db_datei=db_datei, knecht=KNECHT.zustand(), fehler=fehler, sicherungen=sicherung.liste()[:7],
                  sicherung_einst=sicherung.einstellungen(db), punkte=punkte, kosten=kosten_info,
                  eingebaut=_eingebaut_anzeige(db), aufbewahrung=aufbewahrung.lesen(db))


# ---------------------------------------------------------------- Konten
@router.get("/konten", response_class=HTMLResponse)
def konten(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db), fehler: str = "",
           neuer_link: dict | None = None):
    verwalter_ids = set(db.scalars(select(OrgMember.user_id).where(OrgMember.role == "admin")))
    zuletzt = dict(db.execute(select(AuthMethod.user_id, AuthMethod.last_used_at)).all())
    liste = [{"u": u, "verwalter": u.id in verwalter_ids, "zuletzt": zuletzt.get(u.id)}
             for u in db.scalars(select(User).order_by(User.username))]
    return _seite(request, "konten.html", user, db, konten=liste, fehler=fehler, neuer_link=neuer_link)


def _konten_fehler(request: Request, user: User, db: Session, text: str) -> HTMLResponse:
    antwort = konten(request, user, db, fehler=text)
    antwort.status_code = 400
    return antwort


@router.post("/konten", dependencies=[Depends(csrf_pruefen)])
def konto_anlegen(request: Request, username: str = Form(""), display_name: str = Form(""),
                  password: str = Form(""), admin: str = Form(""), user: User = Depends(verwalter),
                  db: Session = Depends(get_db)):
    from app.cli import _neues_konto

    name = username.strip().lower()
    if not name or " " in name or len(name) > 64:
        return _konten_fehler(request, user, db, tr(request)("Benutzername fehlt oder enthält Leerzeichen."))
    if not display_name.strip():
        return _konten_fehler(request, user, db, tr(request)("Bitte einen Anzeigenamen angeben."))
    if len(password) < MIN_PASSWORT:
        return _konten_fehler(request, user, db, tr(request)("Das Passwort braucht mindestens {n} Zeichen.", n=MIN_PASSWORT))
    if _zu_leicht(password, name, display_name):
        return _konten_fehler(request, user, db, tr(request)("Dieses Passwort ist zu leicht zu erraten."))
    if db.scalar(select(User).where(User.username == name)):
        return _konten_fehler(request, user, db, tr(request)("Den Benutzer „{name}“ gibt es schon.", name=name))
    _neues_konto(db, name, display_name.strip()[:128], password, admin=bool(admin))
    db.commit()
    return _zurueck("/konten", "konto_angelegt")


@router.post("/konten/{user_id}/passwort", dependencies=[Depends(csrf_pruefen)])
def passwort_setzen(request: Request, user_id: str, password: str = Form(""), user: User = Depends(verwalter),
                    db: Session = Depends(get_db)):
    ziel = db.get(User, user_id)
    if ziel is None:
        raise errors.not_found()
    if len(password) < MIN_PASSWORT:
        return _konten_fehler(request, user, db, tr(request)("Das Passwort braucht mindestens {n} Zeichen.", n=MIN_PASSWORT))
    if _zu_leicht(password, ziel.username, ziel.display_name):
        return _konten_fehler(request, user, db, tr(request)("Dieses Passwort ist zu leicht zu erraten."))
    from app import passwortlink

    passwortlink.passwort_setzen(db, ziel, password)
    db.commit()
    if ziel.id == user.id:
        return RedirectResponse("/verwaltung/anmelden?ok=passwort", status_code=303)
    return _zurueck("/konten", "passwort")


@router.post("/konten/{user_id}/link", dependencies=[Depends(csrf_pruefen)])
def passwort_link(request: Request, user_id: str, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    """Einmaliger Link zum Passwort-Setzen (24 h). Wird nur jetzt angezeigt, nicht gespeichert."""
    from app import passwortlink

    ziel = db.get(User, user_id)
    if ziel is None:
        raise errors.not_found()
    token, bis = passwortlink.erzeugen(db, ziel)
    link = str(request.base_url).rstrip("/") + f"/passwort/{token}"
    return konten(request, user, db, neuer_link={"name": ziel.display_name, "link": link, "bis": bis,
                                                 "qr": qr_bild(link)})


@router.post("/konten/{user_id}/verwalter", dependencies=[Depends(csrf_pruefen)])
def verwalter_umschalten(request: Request, user_id: str, user: User = Depends(verwalter),
                         db: Session = Depends(get_db)):
    from app.services import default_organization, ensure_org_member

    ziel = db.get(User, user_id)
    if ziel is None:
        raise errors.not_found()
    if ziel.id == user.id:
        return _konten_fehler(request, user, db, tr(request)("Das eigene Verwalter-Recht lässt sich nicht selbst entziehen."))
    if _ist_verwalter(db, ziel):
        for om in db.scalars(select(OrgMember).where(OrgMember.user_id == ziel.id)):
            om.role = "member"
    else:
        org = default_organization(db)
        ensure_org_member(db, org.id, ziel)
        db.get(OrgMember, (org.id, ziel.id)).role = "admin"
    db.commit()
    return _zurueck("/konten", "verwalter")


@router.post("/konten/{user_id}/loeschen", dependencies=[Depends(csrf_pruefen)])
def konto_loeschen(request: Request, user_id: str, bestaetigung: str = Form(""), user: User = Depends(verwalter),
                   db: Session = Depends(get_db)):
    """Konto einer anderen Person löschen (z. B. auf deren Wunsch nach Art. 17 DSGVO) – dieselbe Löschung wie
    in der App, bestätigt mit dem Benutzernamen statt dem Passwort."""
    from app import konto

    ziel = db.get(User, user_id)
    if ziel is None:
        raise errors.not_found()
    t = tr(request)
    if ziel.id == user.id:
        return _konten_fehler(request, user, db, t("Das eigene Konto löschst du in der App – dort mit Passwort."))
    if bestaetigung.strip().lower() != ziel.username:
        return _konten_fehler(request, user, db,
                              t("Zur Bestätigung bitte den Benutzernamen „{name}“ eingeben.", name=ziel.username))
    try:
        konto.entfernen(db, ziel)
    except errors.ApiError as e:
        db.rollback()
        return _konten_fehler(request, user, db, t(
            "{name} ist die einzige Spielleitung von {titel}. Zuerst in der App eine andere Person zur Spielleitung "
            "machen oder die Kampagne dort löschen.", name=ziel.display_name, titel=e.params.get("titel", "")))
    db.commit()
    return _zurueck("/konten", "konto_geloescht")


# ---------------------------------------------------------------- Transkription (Worker + extern)
def _eingebaut_anzeige(db: Session) -> dict | None:
    """Karte „Eingebauter Worker“: Einstellungen, Zustand, letzte Messung."""
    from app import eingebaut

    if not get_settings().worker_art:
        return None
    knecht = next((k for k in _knechte(db) if k["w"].local), None)
    info = knecht["info"] if knecht else {}
    vram_karte = info.get("vramMb") if isinstance(info.get("vramMb"), int) else None
    from app.einstellungen import llm_konfig

    return {"werte": eingebaut.lesen(db), "knecht": knecht, "vram_karte": vram_karte,
            "ollama": get_settings().ollama_art, "llm_lokal": llm_konfig(db).art == "lokal",
            "llm_bereit": bool(knecht and knecht["info"].get("llm")),
            "regler_max": vram_karte or 24576,
            "messung": eingebaut.messung(db, knecht["w"].id) if knecht else None}


def _docker() -> bool:
    from app.aktualisierung import docker

    return docker()


def _knechte(db: Session) -> list[dict]:
    grenze = _online_grenze()
    auftraege = {j.lease_worker_id: j for j in db.scalars(select(Job).where(Job.state == "leased"))}
    liste = []
    for w in db.scalars(select(Worker).order_by(Worker.local.desc(), Worker.created_at)):
        job = auftraege.get(w.id)
        sitzung = db.get(GameSession, job.session_id) if job and job.session_id else None
        try:
            info = json.loads(w.info) if w.info else {}
        except ValueError:
            info = {}
        liste.append({
            "w": w, "online": bool(w.last_seen_at and w.last_seen_at > grenze), "info": info,
            "job": job, "sitzung": sitzung,
            "kampagne": db.get(Campaign, sitzung.campaign_id) if sitzung else None,
        })
    return liste


def _extern_anzeige(db: Session) -> dict:
    from app import extern
    from app.einstellungen import extern_konfig

    k = extern_konfig(db)
    return {"anbieter": k.anbieter, "anbieter_name": extern.ANBIETER.get(k.anbieter or ""), "gewaehlt": k.gewaehlt,
            "key_ende": (k.api_key or "")[-4:] if k.api_key else None, "stunden": k.stunden,
            "cent": k.cent_pro_minute, "quelle": k.quelle,
            "kampagnen": db.scalar(select(func.count()).select_from(Campaign)
                                   .where(Campaign.allow_external_transcription.is_(True)))}


@router.get("/transkription", response_class=HTMLResponse)
def transkription(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db),
                  neuer_token: str | None = None, neuer_name: str | None = None, fehler: str = ""):
    from app.einstellungen import meta_lesen

    from app.einstellungen import meta_lesen as _meta
    from app.koppeln import aktueller_code

    return _seite(request, "transkription.html", user, db, knechte=_knechte(db), neuer_token=neuer_token,
                  koppel=aktueller_code(db), server_url=str(request.base_url).rstrip("/"),
                  hf_ende=(_meta(db, "hf.token") or "")[-4:] or None, modell=_modell_anzeige(db),
                  neuer_name=neuer_name, fehler=fehler, lokal=KNECHT.zustand(), log=KNECHT.log_ende(),
                  autostart=meta_lesen(db, META_KEY, "aus"), ki=ki_verfuegbar(), ext=_extern_anzeige(db),
                  docker=_docker(), worker_art=get_settings().worker_art, eingebaut=_eingebaut_anzeige(db))


def _modell_anzeige(db: Session) -> dict:
    from app import modellablage
    from app.einstellungen import meta_lesen

    stand = modellablage.vorhanden(db)
    return {"stand": stand, "mb": (stand.groesse / 1024 ** 2) if stand else 0,
            "fehler": meta_lesen(db, "modellablage.fehler") or "", "spiegel": modellablage.spiegel_nutzbar(),
            "gebraucht": modellablage.gebraucht(db)}


@router.post("/transkription/modell", dependencies=[Depends(csrf_pruefen)])
def modell_holen(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    """Sprechermodell sofort auf den Server laden (sonst übernimmt das die Wartung)."""
    from app import modellablage

    if modellablage.versuchen(db) is None:
        from app.einstellungen import meta_lesen

        return _transkription_fehler(request, user, db, meta_lesen(db, "modellablage.fehler") or "?")
    return _zurueck("/transkription#hf", "modell")


@router.post("/transkription/hf", dependencies=[Depends(csrf_pruefen)])
def hf_speichern(request: Request, hf_token: str = Form(""), user: User = Depends(verwalter),
                 db: Session = Depends(get_db)):
    from app import schluessel

    _ = tr(request)
    token = hf_token.strip()
    if not token:
        return _transkription_fehler(request, user, db, _("Bitte den Zugangsschlüssel eintragen."))
    ergebnis = schluessel.huggingface(token)
    if ergebnis == "falsch":
        return _transkription_fehler(request, user, db, _("Hugging Face lehnt diesen Zugangsschlüssel ab."))
    if ergebnis == "kein_zugang":
        return _transkription_fehler(request, user, db, _("Der Schlüssel stimmt, aber die Modellbedingungen sind noch nicht angenommen."))
    meta_schreiben(db, "hf.token", token)
    meta_schreiben(db, "modellablage.versuch", "")  # gleich neu versuchen
    db.commit()
    return _zurueck("/transkription#hf", "gespeichert")


@router.get("/worker", include_in_schema=False)
def worker_alt():
    return RedirectResponse("/verwaltung/transkription", status_code=301)


def _transkription_fehler(request: Request, user: User, db: Session, text: str) -> HTMLResponse:
    antwort = transkription(request, user, db, fehler=text)
    antwort.status_code = 400
    return antwort


@router.post("/worker", dependencies=[Depends(csrf_pruefen)])
def knecht_anlegen(request: Request, name: str = Form(""), user: User = Depends(verwalter),
                   db: Session = Depends(get_db)):
    from app.routers.worker import token_hash

    _ = tr(request)
    name = name.strip()[:100]
    if not name:
        return _transkription_fehler(request, user, db, _("Bitte einen Namen angeben, z. B. „pc-von-lilio“."))
    if db.scalar(select(Worker).where(Worker.name == name, Worker.revoked_at.is_(None))):
        return _transkription_fehler(request, user, db, _("Einen aktiven Worker „{name}“ gibt es schon.",
                                                          name=name))
    geheim = secrets.token_urlsafe(32)
    w = Worker(name=name, token_hash=token_hash(geheim), capabilities="asr,llm")
    db.add(w)
    db.commit()
    # Schlüssel wird genau einmal angezeigt (nicht als Weiterleitung, damit er nicht in Verlauf/Logs landet)
    return transkription(request, user, db, neuer_token=f"wk.{w.id}.{geheim}", neuer_name=name)


def _knecht(db: Session, worker_id: str) -> Worker:
    w = db.get(Worker, worker_id)
    if w is None:
        raise errors.not_found()
    return w


@router.post("/worker/eingebaut", dependencies=[Depends(csrf_pruefen)])
def eingebaut_speichern(user: User = Depends(verwalter), db: Session = Depends(get_db),
                        vram_mb: str = Form(None), modell: str = Form(None), prozessor: str = Form(None),
                        prozessor_feld: str = Form(None), threads: str = Form(None)):
    from app import eingebaut

    if not get_settings().worker_art:
        raise errors.not_found()
    eingebaut.speichern(db, vram_mb, modell, (prozessor == "1") if prozessor_feld else None, threads)
    db.commit()
    return _zurueck("/transkription", "worker_einstellungen")


@router.post("/worker/eingebaut/neustart", dependencies=[Depends(csrf_pruefen)])
def eingebaut_neustart(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app import eingebaut

    if not get_settings().worker_art:
        raise errors.not_found()
    eingebaut.neu_starten(db)
    db.commit()
    return _zurueck("/transkription", "worker_neustart")


@router.post("/worker/{worker_id}/pause", dependencies=[Depends(csrf_pruefen)])
def knecht_pause(worker_id: str, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    _knecht(db, worker_id).paused = True
    db.commit()
    return _zurueck("/transkription", "pausiert")


@router.post("/worker/{worker_id}/fortsetzen", dependencies=[Depends(csrf_pruefen)])
def knecht_fortsetzen(worker_id: str, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    _knecht(db, worker_id).paused = False
    db.commit()
    return _zurueck("/transkription", "fortgesetzt")


@router.post("/worker/{worker_id}/sperren", dependencies=[Depends(csrf_pruefen)])
def knecht_sperren(worker_id: str, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    w = _knecht(db, worker_id)
    if w.local:
        KNECHT.beenden()
        meta_schreiben(db, META_KEY, "aus")
    w.revoked_at = utcnow()
    db.commit()
    return _zurueck("/transkription", "gesperrt")


@router.post("/worker/lokal/start", dependencies=[Depends(csrf_pruefen)])
def lokal_starten(modus: str = Form("echt"), autostart: str = Form(""), user: User = Depends(verwalter),
                  db: Session = Depends(get_db)):
    if modus not in ("echt", "attrappe"):
        raise errors.bad_request("validation_error")
    meta_schreiben(db, META_KEY, modus if autostart else "aus")
    KNECHT.starten(db, modus)
    return _zurueck("/transkription", "gestartet")


@router.post("/worker/lokal/stop", dependencies=[Depends(csrf_pruefen)])
def lokal_beenden(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    meta_schreiben(db, META_KEY, "aus")
    db.commit()
    KNECHT.beenden()
    return _zurueck("/transkription", "beendet")


@router.post("/transkription/extern", dependencies=[Depends(csrf_pruefen)])
def extern_speichern(request: Request, anbieter: str = Form(""), api_key: str = Form(""), key_loeschen: str = Form(""),
                     stunden: str = Form("24"), user: User = Depends(verwalter), db: Session = Depends(get_db)):
    """Externe Transkription in der Oberfläche einstellen (überschreibt die .env). Der Schlüssel wird nie angezeigt."""
    from app.einstellungen import extern_konfig

    _ = tr(request)
    if anbieter not in ("", "mistral"):
        return _transkription_fehler(request, user, db, _("Unbekannter Anbieter."))
    try:
        h = float(stunden.replace(",", "."))
        if not 0 <= h <= 720:
            raise ValueError
    except ValueError:
        return _transkription_fehler(request, user, db, _("Wartezeit bitte in Stunden zwischen 0 und 720 angeben."))
    key = api_key.strip()
    if key and (len(key) < 16 or len(key) > 200 or any(c.isspace() for c in key)):
        return _transkription_fehler(request, user, db, _("Der API-Schlüssel sieht nicht vollständig aus."))
    if anbieter and not key and not extern_konfig(db).api_key and not key_loeschen:
        return _transkription_fehler(request, user, db, _("Zum Freigeben bitte den API-Schlüssel eintragen."))
    meta_schreiben(db, "extern.anbieter", anbieter)
    meta_schreiben(db, "extern.after_hours", f"{h:g}")
    if key_loeschen:
        meta_schreiben(db, "extern.api_key", "")
    elif key:
        meta_schreiben(db, "extern.api_key", key)
    db.commit()
    return _zurueck("/transkription", "extern")


# ---------------------------------------------------------------- Zusammenfassung (Sprachmodell)
MISTRAL_URL = "https://api.mistral.ai/v1"
TOKENS_JE_SESSION = (110_000, 5_000)  # 4 Stunden Spiel: Recap- und Vorschlags-Aufruf zusammen


def _llm_anzeige(db: Session) -> dict:
    from app.einstellungen import extern_konfig, llm_konfig
    from app.sprachmodell import PREISE
    from app import woerterbuch
    from app.zusammenfassung import gegenpruefen_an

    k = llm_konfig(db)
    preis = (k.cent_ein, k.cent_aus) if k.cent_ein is not None and k.cent_aus is not None \
        else PREISE.get(k.api_modell)
    je_session = (TOKENS_JE_SESSION[0] * preis[0] + TOKENS_JE_SESSION[1] * preis[1]) / 1_000_000 if preis else None
    worker = [kn for kn in _knechte(db) if kn["info"].get("llm") and not kn["w"].revoked_at]
    return {"k": k, "mistral": k.ist_mistral, "key_ende": (k.api_key or "")[-4:] if k.api_key else None,
            "key_von_extern": bool(k.api_key) and not k.eigener_key,
            "extern_key": bool(extern_konfig(db).api_key), "je_session": je_session, "preis": preis,
             "worker": worker, "worker_online": any(kn["online"] and not kn["w"].paused and kn["w"].app_paused_since is None
                                  for kn in worker), "gegenpruefen": gegenpruefen_an(db),
             "wortlisten": {sp: woerterbuch.bereit(sp) for sp in ("de", "en")}}


@router.get("/zusammenfassung", response_class=HTMLResponse)
def zusammenfassung_seite(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db),
                          fehler: str = ""):
    return _seite(request, "zusammenfassung.html", user, db, llm=_llm_anzeige(db), fehler=fehler)


@router.post("/zusammenfassung", dependencies=[Depends(csrf_pruefen)])
def zusammenfassung_speichern(request: Request, art: str = Form("aus"), anbieter: str = Form("mistral"),
                              api_url: str = Form(""), api_modell: str = Form(""), api_key: str = Form(""),
                              key_loeschen: str = Form(""), lokal_modell: str = Form(""),
                              lokal_kontext: str = Form("12288"), cent_ein: str = Form(""), cent_aus: str = Form(""),
                              gegenpruefen_feld: str = Form(""), gegenpruefen: str = Form(""),
                              user: User = Depends(verwalter), db: Session = Depends(get_db)):
    """Wer Recap und Vorschläge schreibt. Überschreibt die .env; der Schlüssel wird nie angezeigt."""
    from app.einstellungen import LLM_ARTEN, llm_konfig

    _ = tr(request)

    def fehler(text: str) -> HTMLResponse:
        antwort = zusammenfassung_seite(request, user, db, fehler=text)
        antwort.status_code = 400
        return antwort

    if art not in LLM_ARTEN or anbieter not in ("mistral", "andere"):
        return fehler(_("Unbekannte Auswahl."))
    url = MISTRAL_URL if anbieter == "mistral" else api_url.strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        return fehler(_("Die Adresse muss mit https:// beginnen."))
    modell, lokal = api_modell.strip(), lokal_modell.strip()
    if not modell or not lokal or len(modell) > 100 or len(lokal) > 100:
        return fehler(_("Bitte die Modellnamen angeben."))
    try:
        kontext = int(lokal_kontext)
        if not 4096 <= kontext <= 262144:
            raise ValueError
    except ValueError:
        return fehler(_("Kontextgröße bitte als Zahl zwischen 4096 und 262144 angeben."))
    preise = []
    for wert in (cent_ein, cent_aus):
        wert = wert.strip().replace(",", ".")
        try:
            preise.append("" if not wert else f"{float(wert):g}")
            if wert and not 0 <= float(wert) <= 100000:
                raise ValueError
        except ValueError:
            return fehler(_("Preise bitte als Zahl in Cent angeben."))
    if bool(preise[0]) != bool(preise[1]):
        return fehler(_("Bitte beide Preise angeben oder beide leer lassen."))
    key = api_key.strip()
    if key and (len(key) < 16 or len(key) > 300 or any(c.isspace() for c in key)):
        return fehler(_("Der API-Schlüssel sieht nicht vollständig aus."))
    meta_schreiben(db, "llm.art", art)
    meta_schreiben(db, "llm.api_url", url)
    meta_schreiben(db, "llm.api_modell", modell)
    meta_schreiben(db, "llm.lokal_modell", lokal)
    meta_schreiben(db, "llm.lokal_kontext", str(kontext))
    meta_schreiben(db, "llm.cent_ein", preise[0])
    meta_schreiben(db, "llm.cent_aus", preise[1])
    if gegenpruefen_feld:  # nur, wenn das Formular den Schalter enthält (ältere offene Seiten ändern ihn nicht)
        from app.zusammenfassung import K_GEGENPRUEFEN

        meta_schreiben(db, K_GEGENPRUEFEN, "an" if gegenpruefen else "aus")
    if key_loeschen:
        meta_schreiben(db, "llm.api_key", "")
    elif key:
        meta_schreiben(db, "llm.api_key", key)
    if art == "api" and not llm_konfig(db).api_key:
        db.rollback()
        return fehler(_("Für die API bitte einen API-Schlüssel eintragen."))
    db.commit()
    return _zurueck("/zusammenfassung", "sprachmodell")


# ---------------------------------------------------------------- Warteschlange
@router.get("/warteschlange", response_class=HTMLResponse)
def warteschlange(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db),
                  fehler: str = ""):
    from app.extern import HALTER

    _ = tr(request)
    namen = {w.id: (_("Lokaler Worker") if w.local else w.name) for w in db.scalars(select(Worker))}
    namen |= {"zentrale": _("Server"), HALTER: _("Externer Anbieter")}
    zeilen = []
    for j in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)):
        s = db.get(GameSession, j.session_id) if j.session_id else None
        dok = db.get(CampaignDocument, j.document_id) if j.document_id else None
        c = db.get(Campaign, s.campaign_id) if s else (db.get(Campaign, dok.campaign_id) if dok else None)
        aktuell = s is not None and queue.current_job(db, s.id, None) is j
        zeilen.append({"j": j, "s": s, "c": c, "dok": dok, "knecht": namen.get(j.lease_worker_id or ""),
                       "neustart": aktuell and s.state == "failed",
                       "haengt": j.state == "leased" and j.lease_expires_at is not None
                                 and j.lease_expires_at < utcnow()})
    offen = sum(1 for z in zeilen if z["j"].state in ("queued", "leased"))
    return _seite(request, "warteschlange.html", user, db, zeilen=zeilen, offen=offen, fehler=fehler)


@router.post("/sessions/{session_id}/neustart", dependencies=[Depends(csrf_pruefen)])
def neustart(request: Request, session_id: str, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    s = db.get(GameSession, session_id)
    if s is None:
        raise errors.not_found()
    try:
        queue.neu_starten(db, s)
    except errors.ApiError as e:
        db.rollback()
        antwort = warteschlange(request, user, db, fehler=e.message(sprache_von(request)))
        antwort.status_code = 409
        return antwort
    db.commit()
    return _zurueck("/warteschlange", "neustart")


# ---------------------------------------------------------------- Einstellungen und Sicherung
@router.get("/einstellungen", response_class=HTMLResponse)
def einstellungen(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db),
                  fehler: str = ""):
    from app.konto import registrierung

    orgs = db.scalars(select(Organization).order_by(Organization.created_at)).all()
    from app import kosten

    from app import benachrichtigung

    limit = kosten.limit_cent(db)
    from app import webapp
    from app.config import get_settings

    from app import aufbewahrung

    return _seite(request, "einstellungen.html", user, db, orgs=orgs, fehler=fehler,
                  registrierung=registrierung(db), limit_euro=(limit or 0) / 100,
                  aufbewahrung=aufbewahrung.lesen(db), hoechstens_tage=aufbewahrung.HOECHSTENS_TAGE,
                  zustimmungen=db.scalar(select(func.count()).select_from(Member)
                                         .where(Member.recording_consent_at.is_not(None))) or 0,
                  melden=benachrichtigung.konfig(db), web_zentral=webapp.zentral_erlaubt(db),
                  web_herkuenfte="\n".join(webapp.zusaetzliche(db)),
                  zentrale_webapp=webapp.herkunft(get_settings().central_web_origin))


@router.post("/einstellungen", dependencies=[Depends(csrf_pruefen)])
def einstellungen_speichern(request: Request, server_name: str = Form(""), server_operator: str = Form(""),
                            server_contact: str = Form(""), privacy_policy_url: str = Form(""),
                            min_age: str = Form("16"), org_name: str = Form(""), app_min_version: str = Form(""),
                            app_latest_version: str = Form(""), app_download_url: str = Form(""),
                            app_release_notes: str = Form(""), registrierung: str = Form(""), limit_euro: str = Form(""),
                            public_url: str = Form(""), web_zentral: str = Form(""), web_zentral_feld: str = Form(""),
                            web_herkuenfte: str = Form(""), audio_modus: str = Form(""),
                            audio_tage: str = Form(""), audio_bestaetigt: str = Form(""),
                            user: User = Depends(verwalter),
                            db: Session = Depends(get_db)):
    from app.einstellungen import mindestversion_vergessen, version_tupel

    _ = tr(request)

    def fehler(text: str):
        antwort = einstellungen(request, user, db, fehler=text)
        antwort.status_code = 400
        return antwort

    if not server_name.strip() or not server_operator.strip():
        return fehler(_("Name des Servers und Betreiber dürfen nicht leer sein."))
    url = privacy_policy_url.strip()
    download = app_download_url.strip()
    oeffentlich = public_url.strip().rstrip("/")
    for link in (url, download, oeffentlich):
        if link and not link.startswith(("https://", "http://")):
            return fehler(_("Links müssen mit https:// beginnen."))
    try:
        alter = int(min_age)
        if not 0 <= alter <= 99:
            raise ValueError
    except ValueError:
        return fehler(_("Mindestalter bitte als Zahl angeben (z. B. 16)."))
    if registrierung not in ("", "invite_only", "closed"):
        return fehler(_("Unbekannte Auswahl."))
    if limit_euro.strip():
        try:
            euro = float(limit_euro.replace(",", "."))
            if not 0 <= euro <= 10000:
                raise ValueError
        except ValueError:
            return fehler(_("Das Monatslimit bitte als Betrag in Euro angeben (0 = ohne Limit)."))
        meta_schreiben(db, "kosten.limit_cent", str(int(round(euro * 100))))
    for v in (app_min_version, app_latest_version):
        if v.strip() and version_tupel(v) is None:
            return fehler(_("Versionen bitte als Zahlen mit Punkten angeben, z. B. 0.9.0."))
    from app import aufbewahrung

    alt_frist = aufbewahrung.lesen(db)
    neue_frist = alt_frist
    if audio_modus:
        try:
            neue_frist = aufbewahrung.Aufbewahrung(audio_modus, int(audio_tage or alt_frist.tage))
            if neue_frist.modus not in aufbewahrung.MODI or not 1 <= neue_frist.tage <= aufbewahrung.HOECHSTENS_TAGE:
                raise ValueError
        except ValueError:
            return fehler(_("Aufnahmen: bitte höchstens {n} Tage angeben.", n=aufbewahrung.HOECHSTENS_TAGE))
        if neue_frist.laenger_als(alt_frist) and audio_bestaetigt != "ja":
            return fehler(_("Die Aufnahmen sollen länger bleiben. Dann müssen alle Mitglieder neu zustimmen – bitte das Kästchen dazu ankreuzen."))
    speichern(db, server_name=server_name.strip()[:100], server_operator=server_operator.strip()[:200],
              server_contact=server_contact.strip()[:200] or None, privacy_policy_url=url[:500] or None,
              min_age=alter, app_min_version=app_min_version.strip() or None,
              app_latest_version=app_latest_version.strip() or None, app_download_url=download[:500] or None,
              app_release_notes=app_release_notes.strip()[:1000] or None, public_url=oeffentlich[:300] or None)
    if registrierung:
        meta_schreiben(db, "registrierung", registrierung)
    if web_zentral_feld:  # Kästchen war auf der Seite (sonst gar nicht angezeigt)
        meta_schreiben(db, "web.zentral", "an" if web_zentral == "an" else "aus")
    from app.webapp import herkunft

    zeilen = [z.strip() for z in web_herkuenfte.splitlines() if z.strip()]
    if any(herkunft(z) is None for z in zeilen):
        return fehler(_("Weitere Adressen bitte vollständig angeben, z. B. https://taleward.meinverein.de"))
    meta_schreiben(db, "web.herkuenfte", "\n".join(herkunft(z) for z in zeilen[:20]))
    zurueckgesetzt = 0
    geaendert = neue_frist.api() != alt_frist.api()
    if geaendert:
        zurueckgesetzt = aufbewahrung.speichern(db, neue_frist.modus, neue_frist.tage, user.id)
    orgs = db.scalars(select(Organization).order_by(Organization.created_at)).all()
    if org_name.strip() and len(orgs) == 1:
        orgs[0].name = org_name.strip()[:200]
    db.commit()
    mindestversion_vergessen()
    from app import webapp

    webapp.vergessen()
    if geaendert:
        return _zurueck("/einstellungen#aufnahmen", "aufbewahrung_zustimmung" if zurueckgesetzt else "aufbewahrung")
    return _zurueck("/einstellungen", "gespeichert")


@router.post("/benachrichtigungen", dependencies=[Depends(csrf_pruefen)])
def benachrichtigungen_speichern(request: Request, ntfy_url: str = Form(""), ntfy_token: str = Form(""),
                                 smtp_host: str = Form(""), smtp_port: str = Form("587"),
                                 smtp_tls: str = Form("starttls"), smtp_user: str = Form(""),
                                 smtp_pass: str = Form(""), email_von: str = Form(""), email_an: str = Form(""),
                                 sprache: str = Form("de"), stunden: str = Form("6"),
                                 zugang_loeschen: str = Form(""), aktion: str = Form(""),
                                 user: User = Depends(verwalter),
                                 db: Session = Depends(get_db)):
    """ntfy und/oder E-Mail für Meldungen an den Betreiber. Token und Passwort sind nur schreibbar."""
    _ = tr(request)

    def fehler(text: str):
        antwort = einstellungen(request, user, db, fehler=text)
        antwort.status_code = 400
        return antwort

    ntfy_url = ntfy_url.strip()
    if ntfy_url:
        teile = urlsplit(ntfy_url)
        if teile.scheme not in ("https", "http") or not teile.netloc or len(teile.path.strip("/")) < 1:
            return fehler(_("Die ntfy-Adresse bitte vollständig angeben, z. B. https://ntfy.sh/mein-verein-taleward."))
    try:
        port = int(smtp_port or "587")
        std = int(stunden or "6")
        if not (1 <= port <= 65535 and 1 <= std <= 168):
            raise ValueError
    except ValueError:
        return fehler(_("Port bitte als Zahl, Wartezeit in Stunden zwischen 1 und 168."))
    if smtp_tls not in ("starttls", "ssl", "aus") or sprache not in ("de", "en"):
        return fehler(_("Unbekannte Auswahl."))
    email_an = email_an.strip()
    if email_an and "@" not in email_an:
        return fehler(_("Das sieht nicht nach einer E-Mail-Adresse aus."))
    werte = {"ntfy_url": ntfy_url[:500], "smtp_host": smtp_host.strip()[:200], "smtp_port": str(port),
             "smtp_tls": smtp_tls, "smtp_user": smtp_user.strip()[:200], "email_von": email_von.strip()[:200],
             "email_an": email_an[:200], "sprache": sprache, "stunden": str(std)}
    for k, v in werte.items():
        meta_schreiben(db, f"melden.{k}", v)
    if zugang_loeschen:
        meta_schreiben(db, "melden.ntfy_token", "")
        meta_schreiben(db, "melden.smtp_pass", "")
    if ntfy_token.strip():
        meta_schreiben(db, "melden.ntfy_token", ntfy_token.strip()[:500])
    if smtp_pass:
        meta_schreiben(db, "melden.smtp_pass", smtp_pass[:500])
    db.commit()
    if aktion != "test":
        return _zurueck("/einstellungen#benachrichtigungen", "melden")
    from app import benachrichtigung

    if not benachrichtigung.konfig(db).aktiv:
        return fehler(_("Erst ntfy oder E-Mail eintragen."))
    probleme = benachrichtigung.melden(db, "test", wichtig=False)
    if probleme:
        antwort = fehler(_("Gespeichert, aber nicht zugestellt: {grund}", grund="; ".join(probleme)))
        antwort.status_code = 502
        return antwort
    return _zurueck("/einstellungen#benachrichtigungen", "melden_test")


@router.post("/sicherung", dependencies=[Depends(csrf_pruefen)])
def sicherung_jetzt(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    """Sofort eine vollständige Sicherung anlegen (liegt danach in der Liste zum Herunterladen)."""
    from app import sicherung

    e = sicherung.einstellungen(db)
    sicherung.erstellen("manuell", e["ordner"], e["tage"])
    from app import benachrichtigung

    benachrichtigung.sicherung_gelungen(db)
    return _zurueck("/#sicherung", "gesichert")


@router.get("/sicherungen/{name}")
def sicherung_herunterladen(name: str, user: User = Depends(verwalter)):
    from app import sicherung

    p = sicherung.pfad(name)
    if p is None:
        raise errors.not_found()
    return FileResponse(p, filename=name, media_type="application/zip")


@router.post("/sicherung/einstellungen", dependencies=[Depends(csrf_pruefen)])
def sicherung_einstellen(request: Request, auto: str = Form(""), tage: str = Form("14"), ordner: str = Form(""),
                         weiter: str = Form(""), user: User = Depends(verwalter), db: Session = Depends(get_db)):
    _ = tr(request)
    try:
        n = int(tage)
        if not 1 <= n <= 365:
            raise ValueError
    except ValueError:
        return _fehlerseite(request, user, db, _("Aufbewahrung bitte in Tagen zwischen 1 und 365 angeben."))
    ordner = ordner.strip()
    if ordner:
        pfad = Path(ordner)
        if not pfad.is_absolute() or not pfad.is_dir() or not os.access(pfad, os.W_OK):
            return _fehlerseite(request, user, db, _("Der zusätzliche Ordner existiert nicht oder ist nicht beschreibbar."))
    meta_schreiben(db, "sicherung.auto", "an" if auto else "aus")
    meta_schreiben(db, "sicherung.tage", str(n))
    meta_schreiben(db, "sicherung.ordner", ordner)
    db.commit()
    if weiter:
        return RedirectResponse(weiter if weiter.startswith("/verwaltung/") else "/verwaltung/", status_code=303)
    return _zurueck("/#sicherung", "gespeichert")


def _fehlerseite(request: Request, user: User, db: Session, text: str) -> HTMLResponse:
    antwort = uebersicht(request, user, db, fehler=text)
    antwort.status_code = 400
    return antwort


# ---------------------------------------------------------------- Öffentliche Seiten (ohne Anmeldung)
seiten = APIRouter(include_in_schema=False)


def qr_bild(text: str) -> str:
    """QR-Code als data:-SVG (ohne Skript, CSP-konform). Immer dunkel auf hell, damit Kameras ihn auch im dunklen
    Thema lesen."""
    import segno

    return segno.make(text, error="m").svg_data_uri(scale=6, border=3, dark="#2A2118", light="#FBF6EA")


EINLADUNG_FEHLVERSUCHE, EINLADUNG_FENSTER = 30, 15 * 60  # falsche Codes je Adresse


@seiten.get("/einladung/{code}", response_class=HTMLResponse)
def einladung(code: str, request: Request, db: Session = Depends(get_db)):
    """Landeseite eines Einladungslinks für Leute ohne App. Zeigt keinen Kampagnentitel (die Seite ist öffentlich)."""
    from urllib.parse import quote, urlsplit

    from app.models import Invite
    from app.services import normalize_invite_code

    from app import webapp
    from app.einstellungen import oeffentliche_adresse

    from app.begrenzung import ZAEHLER

    code = normalize_invite_code(code)[:32]
    adresse = request.client.host if request.client else "?"
    einladung = None
    if not ZAEHLER.voll(f"einladung-adr:{adresse}", EINLADUNG_FEHLVERSUCHE, EINLADUNG_FENSTER):
        einladung = db.get(Invite, code)
        if einladung is None:
            ZAEHLER.zaehlen(f"einladung-adr:{adresse}", EINLADUNG_FENSTER)
    gueltig = einladung is not None and einladung.expires_at > utcnow()
    link = str(request.base_url).rstrip("/") + f"/einladung/{code}"
    a = angaben(db)
    ziel = urlsplit(a.app_download_url or "")
    store = ziel.netloc.endswith(("play.google.com", "apps.apple.com"))
    antwort = _seite(request, "einladung.html", None, db, code=code, gueltig=gueltig,
                     bis=einladung.expires_at if gueltig else None, link=link,
                     app_link="taleward://einladung?url=" + quote(link, safe=""),
                     apk_hinweis=ziel.path.lower().endswith(".apk") or not store,
                     og_bild=str(request.base_url).rstrip("/") + "/verwaltung/static/marke/og-image.png",
                     qr=qr_bild(link) if gueltig else None,
                     browser_link=webapp.browser_link(db, oeffentliche_adresse(db, request), link) if gueltig else None)
    if not gueltig:
        antwort.status_code = 404
    return antwort


@seiten.get("/passwort/{token}", response_class=HTMLResponse)
def passwort_seite(token: str, request: Request, db: Session = Depends(get_db)):
    from app import passwortlink

    ziel = passwortlink.pruefen(db, token)
    antwort = _seite(request, "passwort.html", None, db, gueltig=ziel is not None, token=token,
                     konto=ziel.username if ziel else "", fertig=False, fehler="")
    if ziel is None:
        antwort.status_code = 404
    return antwort


@seiten.post("/passwort/{token}", response_class=HTMLResponse)
def passwort_seite_setzen(token: str, request: Request, password: str = Form(""), password2: str = Form(""),
                          db: Session = Depends(get_db)):
    """Ohne Anmeldung, daher ohne CSRF-Sitzung: Der Link selbst ist das Geheimnis (einmalig, 24 h)."""
    from app import passwortlink

    _ = tr(request)
    ziel = passwortlink.pruefen(db, token)
    if ziel is None:
        return passwort_seite(token, request, db)
    fehler = ""
    if len(password) < MIN_PASSWORT:
        fehler = _("Das Passwort braucht mindestens {n} Zeichen.", n=MIN_PASSWORT)
    elif _zu_leicht(password, ziel.username, ziel.display_name):
        fehler = _("Dieses Passwort ist zu leicht zu erraten.")
    elif password != password2:
        fehler = _("Die beiden Passwörter stimmen nicht überein.")
    if fehler:
        antwort = _seite(request, "passwort.html", None, db, gueltig=True, token=token, konto=ziel.username,
                         fertig=False, fehler=fehler)
        antwort.status_code = 400
        return antwort
    passwortlink.einloesen(db, token, password)
    return _seite(request, "passwort.html", None, db, gueltig=True, token="", konto=ziel.username, fertig=True,
                  fehler="")


@seiten.get("/verwaltung/sprache/{code}")
def sprache_waehlen(code: str, weiter: str = "/verwaltung/"):
    """Sprache der Weboberfläche wählen (Cookie für ein Jahr). Nur Rücksprung auf eigene Seiten."""
    if not weiter.startswith(("/verwaltung", "/einladung")) or "//" in weiter or "\\" in weiter:
        weiter = "/verwaltung/"
    antwort = RedirectResponse(weiter, status_code=303)
    if code in SPRACHEN:
        antwort.set_cookie("tw_sprache", code, max_age=365 * 24 * 3600, samesite="lax", path="/")
    return antwort


# ---------------------------------------------------------------- Einbinden
SICHERHEITS_KOPFZEILEN = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}

# Web-App unter /app/ (gleiche Herkunft wie /verwaltung): nur eigene Skripte, keine Einbettung. Verbindungen und
# Bilder auch zu anderen Taleward-Servern (die App kann mehrere verbinden), Stil-Attribute von React erlaubt.
WEBAPP_KOPFZEILEN = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob: https:; media-src 'self' data: blob: https:; "
                               "connect-src 'self' https: http://localhost:* http://127.0.0.1:*; "
                               "font-src 'self' data:; worker-src 'self' blob:; manifest-src 'self'; "
                               "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    "X-Frame-Options": "DENY",
}
HSTS = "max-age=31536000"


def einbinden(app: FastAPI) -> None:
    from app.verwaltung import anmeldung, assistent, uebergabe

    app.include_router(seiten)
    app.include_router(uebergabe.router)
    app.include_router(anmeldung.router)
    from app.verwaltung import updates

    app.include_router(updates.router)
    app.include_router(assistent.router)
    app.include_router(assistent.oeffentlich)
    app.include_router(router)
    app.mount("/verwaltung/static", StaticFiles(directory=str(HIER / "static")), name="verwaltung-static")

    @app.exception_handler(NichtAngemeldet)
    async def _nicht_angemeldet(_req: Request, _exc: NichtAngemeldet):
        return RedirectResponse("/verwaltung/anmelden", status_code=303)

    @app.exception_handler(EinrichtungNoetig)
    async def _einrichtung_noetig(_req: Request, _exc: EinrichtungNoetig):
        return RedirectResponse("/verwaltung/einrichtung", status_code=303)

    @app.middleware("http")
    async def _kopfzeilen(request: Request, call_next):
        antwort: Response = await call_next(request)
        pfad = request.url.path
        if pfad.startswith("/verwaltung/static/"):
            antwort.headers.setdefault("Cache-Control", "public, max-age=86400")  # Stil und Logo dürfen zwischengespeichert werden
            antwort.headers.setdefault("X-Content-Type-Options", "nosniff")
        elif pfad.startswith(("/verwaltung", "/einladung", "/datenschutz", "/passwort", "/konto/")):
            for k, v in SICHERHEITS_KOPFZEILEN.items():
                antwort.headers.setdefault(k, v)
        elif pfad == "/app" or pfad.startswith("/app/"):
            for k, v in WEBAPP_KOPFZEILEN.items():
                antwort.headers.setdefault(k, v)
        if request.url.scheme == "https":  # hinter Caddy (X-Forwarded-Proto); im Heimnetz ohne TLS nicht
            antwort.headers.setdefault("Strict-Transport-Security", HSTS)
        return antwort

    @app.get("/", include_in_schema=False)
    def _start():
        return RedirectResponse("/verwaltung", status_code=307)

    # Browser und Handys fragen diese Symbole ohne Verweis direkt beim Server an
    marke = HIER / "static" / "marke"

    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon():
        return FileResponse(marke / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def _touch_icon():
        return FileResponse(marke / "apple-touch-icon.png", media_type="image/png",
                            headers={"Cache-Control": "public, max-age=604800"})
