"""Einrichtungsassistent der Verwaltung.

1. Einrichtung mit Code (statt admin/admin): ersten Verwalter anlegen.
2. Geführte Schritte: Verein → Betriebsart (Cloud, lokaler Server, beides; Schlüssel werden sofort geprüft,
   Worker per Code koppeln) → Zusammenfassung → Datenschutzhinweis → Sicherung → erste Spielleitung.
Jeder Schritt lässt sich überspringen und später wieder aufrufen; die Übersicht zeigt, was noch offen ist
(einrichtung.stand – aus dem tatsächlichen Zustand, nicht aus Häkchen).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import einrichtung, errors, kosten, schluessel
from app.db import get_db, utcnow
from app.einstellungen import angaben, extern_konfig, llm_konfig, meta_lesen, meta_schreiben, speichern
from app.models import Organization, OrgMember, User, Worker
from app.verwaltung.router import (
    COOKIE, MIN_PASSWORT, SITZUNG_STUNDEN, NichtAngemeldet, _cookie_wert, _fehlversuche, _gesperrt, _online_grenze,
    _seite, csrf_pruefen, sitzung, tr, verwalter,
)

router = APIRouter(prefix="/verwaltung", include_in_schema=False)
oeffentlich = APIRouter(include_in_schema=False)

SCHRITTE = [("verein", "Verein"), ("betrieb", "Betriebsart"), ("zusammenfassung", "Zusammenfassung"),
            ("datenschutz", "Datenschutz"), ("sicherung", "Sicherung"), ("spielleitung", "Spielleitung")]
MISTRAL_URL = "https://api.mistral.ai/v1"
LEICHT = ("admin", "passwort", "password", "12345678", "taleward")


def _weiter(schritt: str) -> RedirectResponse:
    namen = [s for s, _ in SCHRITTE]
    i = namen.index(schritt)
    ziel = f"/verwaltung/assistent/{namen[i + 1]}" if i + 1 < len(namen) else "/verwaltung/assistent/fertig"
    return RedirectResponse(ziel, status_code=303)


def _anmelden(antwort: RedirectResponse, request: Request, db: Session, user: User) -> RedirectResponse:
    antwort.set_cookie(COOKIE, _cookie_wert(db, user), max_age=SITZUNG_STUNDEN * 3600, httponly=True,
                       samesite="strict", secure=request.url.scheme == "https", path="/verwaltung")
    return antwort


def _passwort_fehler(_, name: str, password: str, password2: str) -> str | None:
    if len(password) < MIN_PASSWORT:
        return _("Das Passwort braucht mindestens {n} Zeichen.", n=MIN_PASSWORT)
    if password != password2:
        return _("Die beiden Passwörter stimmen nicht überein.")
    if password.lower() in (*LEICHT, name):
        return _("Dieses Passwort ist zu leicht zu erraten.")
    return None


# ---------------------------------------------------------------- 1. Einrichtung mit Code
def _alte_sitzung(request: Request, db: Session) -> User | None:
    """Ältere Installationen: angemeldet mit dem Einrichtungskonto admin/admin."""
    try:
        u = sitzung(request, db)
    except NichtAngemeldet:
        return None
    return u if u.setup_account else None


@router.get("/einrichtung", response_class=HTMLResponse)
def einrichtung_seite(request: Request, code: str = "", db: Session = Depends(get_db), fehler: str = ""):
    alt = _alte_sitzung(request, db)
    if alt is None and not einrichtung.braucht_einrichtung(db):
        return RedirectResponse("/verwaltung/anmelden", status_code=303)
    if not code and alt is None and einrichtung.heimnetz_ohne_code(request):
        code = einrichtung.code_erzeugen(db)  # Taleward-Box: Code wird still ins Formular gelegt
    gueltig = alt is not None or einrichtung.code_pruefen(db, code)
    return _seite(request, "einrichtung.html", None, db, fehler=fehler, code=code if gueltig else "",
                  code_falsch=bool(code) and not gueltig, alt=alt is not None,
                  csrf_wert=getattr(request.state, "csrf", ""))


@router.post("/einrichtung")
def einrichtung_abschliessen(request: Request, code: str = Form(""), username: str = Form(""),
                             display_name: str = Form(""), password: str = Form(""), password2: str = Form(""),
                             csrf: str = Form(""), db: Session = Depends(get_db)):
    from app.cli import _neues_konto

    _ = tr(request)
    alt = _alte_sitzung(request, db)
    if alt is not None:
        csrf_pruefen(request, csrf)
    else:
        schluessel_ = f"einrichtung|{request.client.host if request.client else '?'}"
        if _gesperrt(schluessel_):
            return einrichtung_seite(request, "", db, _("Zu viele Fehlversuche. Bitte in ein paar Minuten erneut versuchen."))
        if not einrichtung.code_pruefen(db, code):
            _fehlversuche[schluessel_].append(time.monotonic())
            antwort = einrichtung_seite(request, code, db)
            antwort.status_code = 400
            return antwort

    def fehler(text: str):
        antwort = einrichtung_seite(request, code, db, text)
        antwort.status_code = 400
        return antwort

    name = username.strip().lower()
    if not name or " " in name or len(name) > 64:
        return fehler(_("Benutzername fehlt oder enthält Leerzeichen."))
    if name == "admin":
        return fehler(_("Bitte einen eigenen Benutzernamen wählen, nicht „admin“."))
    if not display_name.strip():
        return fehler(_("Bitte einen Anzeigenamen angeben."))
    if (text := _passwort_fehler(_, name, password, password2)):
        return fehler(text)
    if db.scalar(select(User).where(User.username == name)):
        return fehler(_("Den Benutzer „{name}“ gibt es schon.", name=name))
    neu = _neues_konto(db, name, display_name.strip()[:128], password, admin=True)
    if alt is not None:
        for om in db.scalars(select(OrgMember).where(OrgMember.user_id == alt.id)):
            db.delete(om)
        db.delete(alt)
    einrichtung.code_verbrauchen(db)
    db.commit()
    return _anmelden(RedirectResponse("/verwaltung/assistent/verein", status_code=303), request, db, neu)


# ---------------------------------------------------------------- 2. Schritte
def _schritt(request: Request, user: User, db: Session, schritt: str, fehler: str = "", **ctx) -> HTMLResponse:
    namen = [s for s, _ in SCHRITTE]
    return _seite(request, "assistent.html", user, db, schritt=schritt, schritte=SCHRITTE,
                  nummer=namen.index(schritt) + 1 if schritt in namen else len(namen), fehler=fehler, **ctx)


def _fehler(request, user, db, schritt, fehlertext, **ctx) -> HTMLResponse:
    antwort = _schritt(request, user, db, schritt, fehlertext, **ctx)
    antwort.status_code = 400
    return antwort


@router.get("/assistent", response_class=HTMLResponse)
def assistent_start(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    offen = next((p for p in einrichtung.stand(db) if not p.erledigt), None)
    return RedirectResponse(offen.link if offen else "/verwaltung/assistent/verein", status_code=303)


# -- Verein
@router.get("/assistent/verein", response_class=HTMLResponse)
def verein(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app.config import get_settings

    s, a = get_settings(), angaben(db)
    # Standardwerte aus der Installation nicht vorschlagen – sonst heißt jeder Server „Session-Chronik“
    return _schritt(request, user, db, "verein",
                    verein_wert="" if a.server_operator == s.server_operator else a.server_operator,
                    name_wert="" if a.server_name == s.server_name else a.server_name)


@router.post("/assistent/verein", dependencies=[Depends(csrf_pruefen)])
def verein_speichern(request: Request, verein_name: str = Form(""), server_name: str = Form(""),
                     server_contact: str = Form(""), min_age: str = Form("16"), user: User = Depends(verwalter),
                     db: Session = Depends(get_db)):
    _ = tr(request)
    if not verein_name.strip():
        return _fehler(request, user, db, "verein", _("Bitte den Namen des Vereins angeben."))
    kontakt = server_contact.strip()
    if not kontakt:
        return _fehler(request, user, db, "verein", _("Bitte eine Kontaktadresse für Datenschutzfragen angeben."))
    try:
        alter = int(min_age)
        if not 0 <= alter <= 99:
            raise ValueError
    except ValueError:
        return _fehler(request, user, db, "verein", _("Mindestalter bitte als Zahl angeben (z. B. 16)."))
    speichern(db, server_name=(server_name.strip() or verein_name.strip())[:100],
              server_operator=verein_name.strip()[:200], server_contact=kontakt[:200], min_age=alter)
    orgs = db.scalars(select(Organization).order_by(Organization.created_at)).all()
    if len(orgs) == 1:
        orgs[0].name = verein_name.strip()[:200]
    db.commit()
    return _weiter("verein")


# -- Betriebsart
def _betrieb_ctx(db: Session) -> dict:
    from app.koppeln import aktueller_code

    ext = extern_konfig(db)
    grenze = _online_grenze()
    worker = [{"w": w, "online": bool(w.last_seen_at and w.last_seen_at > grenze)}
              for w in db.scalars(select(Worker).where(Worker.revoked_at.is_(None), Worker.local.is_(False))
                                  .order_by(Worker.created_at))]
    limit = kosten.limit_cent(db)
    return {"art": einrichtung.betriebsart(db), "mistral_ende": (ext.api_key or "")[-4:] if ext.api_key else None,
            "hf_ende": (meta_lesen(db, "hf.token") or "")[-4:] or None, "koppel": aktueller_code(db),
            "worker": worker, "limit_euro": (limit or 2000) / 100, "stunden": ext.stunden if ext.stunden else 12,
            "server_url": ""}


@router.get("/assistent/betrieb", response_class=HTMLResponse)
def betrieb(request: Request, neu: int = 0, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    ctx = _betrieb_ctx(db)
    ctx["server_url"] = str(request.base_url).rstrip("/")
    if neu:
        ctx["art"] = None
    return _schritt(request, user, db, "betrieb", **ctx)


@router.post("/assistent/betrieb/art", dependencies=[Depends(csrf_pruefen)])
def betrieb_art(art: str = Form(""), user: User = Depends(verwalter), db: Session = Depends(get_db)):
    if art in einrichtung.BETRIEBSARTEN:
        meta_schreiben(db, "betrieb.art", art)
        db.commit()
    return RedirectResponse("/verwaltung/assistent/betrieb", status_code=303)


@router.post("/assistent/betrieb", dependencies=[Depends(csrf_pruefen)])
def betrieb_speichern(request: Request, mistral_key: str = Form(""), hf_token: str = Form(""),
                      limit_euro: str = Form(""), stunden: str = Form("12"), user: User = Depends(verwalter),
                      db: Session = Depends(get_db)):
    _ = tr(request)
    art = einrichtung.betriebsart(db)
    ctx = _betrieb_ctx(db) | {"server_url": str(request.base_url).rstrip("/")}

    def fehler(text):
        return _fehler(request, user, db, "betrieb", text, **ctx)

    if art is None:
        return RedirectResponse("/verwaltung/assistent/betrieb", status_code=303)
    hinweise = []
    if art in ("cloud", "beides"):
        key = mistral_key.strip()
        if not key and not extern_konfig(db).api_key:
            return fehler(_("Bitte den API-Schlüssel von Mistral eintragen."))
        if key:
            ergebnis = schluessel.mistral(key)
            if ergebnis == "falsch":
                return fehler(_("Mistral lehnt diesen API-Schlüssel ab. Bitte in console.mistral.ai prüfen."))
            if ergebnis == "unbekannt":
                hinweise.append("mistral")
            meta_schreiben(db, "extern.api_key", key)
        try:
            euro = float((limit_euro or "0").replace(",", "."))
            if not 0 <= euro <= 10000:
                raise ValueError
        except ValueError:
            return fehler(_("Das Monatslimit bitte als Betrag in Euro angeben (0 = ohne Limit)."))
        meta_schreiben(db, "kosten.limit_cent", str(int(round(euro * 100))))
        h = 0.0
        if art == "beides":
            try:
                h = float(stunden.replace(",", "."))
                if not 1 <= h <= 720:
                    raise ValueError
            except ValueError:
                return fehler(_("Wartezeit bitte in Stunden zwischen 1 und 720 angeben."))
        meta_schreiben(db, "extern.anbieter", "mistral")
        meta_schreiben(db, "extern.after_hours", f"{h:g}")
    else:
        meta_schreiben(db, "extern.anbieter", "")
    if art in ("lokal", "beides"):
        token = hf_token.strip()
        if token:
            ergebnis = schluessel.huggingface(token)
            if ergebnis == "falsch":
                return fehler(_("Hugging Face lehnt diesen Zugangsschlüssel ab."))
            if ergebnis == "kein_zugang":
                return fehler(_("Der Schlüssel stimmt, aber die Modellbedingungen sind noch nicht angenommen."))
            if ergebnis == "unbekannt":
                hinweise.append("hf")
            meta_schreiben(db, "hf.token", token)
    db.commit()
    antwort = _weiter("betrieb")
    if hinweise:
        antwort.headers["location"] += "?hinweis=" + ",".join(hinweise)
    return antwort


@router.post("/assistent/koppeln", dependencies=[Depends(csrf_pruefen)])
def koppeln_code(weiter: str = Form(""), user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app.koppeln import code_erzeugen

    code_erzeugen(db)
    ziel = weiter if weiter in ("/verwaltung/assistent/betrieb", "/verwaltung/transkription") else \
        "/verwaltung/transkription"
    return RedirectResponse(ziel + "#koppeln", status_code=303)


# -- Zusammenfassung
@router.get("/assistent/zusammenfassung", response_class=HTMLResponse)
def zusammenfassung(request: Request, hinweis: str = "", user: User = Depends(verwalter),
                    db: Session = Depends(get_db)):
    return _schritt(request, user, db, "zusammenfassung", art=einrichtung.betriebsart(db), llm=llm_konfig(db),
                    mistral=bool(extern_konfig(db).api_key), hinweis=hinweis)


@router.post("/assistent/zusammenfassung", dependencies=[Depends(csrf_pruefen)])
def zusammenfassung_speichern(request: Request, wahl: str = Form(""), mistral_key: str = Form(""),
                              user: User = Depends(verwalter), db: Session = Depends(get_db)):
    _ = tr(request)
    if wahl == "api":
        key = mistral_key.strip()
        if key:
            if schluessel.mistral(key) == "falsch":
                return _fehler(request, user, db, "zusammenfassung",
                               _("Mistral lehnt diesen API-Schlüssel ab. Bitte in console.mistral.ai prüfen."),
                               art=einrichtung.betriebsart(db), llm=llm_konfig(db), mistral=False, hinweis="")
            meta_schreiben(db, "extern.api_key", key)
        if not extern_konfig(db).api_key and not llm_konfig(db).api_key:
            return _fehler(request, user, db, "zusammenfassung", _("Bitte den API-Schlüssel von Mistral eintragen."),
                           art=einrichtung.betriebsart(db), llm=llm_konfig(db), mistral=False, hinweis="")
        meta_schreiben(db, "llm.art", "api")
        meta_schreiben(db, "llm.api_url", MISTRAL_URL)
        if not meta_lesen(db, "llm.api_modell"):
            meta_schreiben(db, "llm.api_modell", "mistral-large-latest")
    elif wahl == "lokal":
        meta_schreiben(db, "llm.art", "lokal")
    db.commit()
    return _weiter("zusammenfassung")


# -- Datenschutz
@router.get("/assistent/datenschutz", response_class=HTMLResponse)
def datenschutz(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app.datenschutz import vorlage

    text = meta_lesen(db, "datenschutz.text") or vorlage(db)
    eigen = angaben(db).privacy_policy_url
    return _schritt(request, user, db, "datenschutz", text=text,
                    eigener_link=eigen if eigen and not eigen.endswith("/datenschutz") else "",
                    veroeffentlicht=bool(meta_lesen(db, "datenschutz.text")))


@router.post("/assistent/datenschutz", dependencies=[Depends(csrf_pruefen)])
def datenschutz_speichern(request: Request, aktion: str = Form(""), text: str = Form(""),
                          eigener_link: str = Form(""), user: User = Depends(verwalter),
                          db: Session = Depends(get_db)):
    from app.datenschutz import vorlage

    _ = tr(request)

    def fehler(t, inhalt):
        return _fehler(request, user, db, "datenschutz", t, text=inhalt, eigener_link=eigener_link,
                       veroeffentlicht=bool(meta_lesen(db, "datenschutz.text")))

    if aktion == "neu":
        meta_schreiben(db, "datenschutz.text", "")
        db.commit()
        return _schritt(request, user, db, "datenschutz", text=vorlage(db), eigener_link="", veroeffentlicht=False)
    if aktion == "link":
        link = eigener_link.strip()
        if not link.startswith("https://"):
            return fehler(_("Links müssen mit https:// beginnen."), text)
        speichern(db, privacy_policy_url=link[:500])
        db.commit()
        return _weiter("datenschutz")
    inhalt = text.strip()
    if len(inhalt) < 200:
        return fehler(_("Der Datenschutzhinweis ist zu kurz."), text)
    offen = inhalt.count("[")
    if offen:
        return fehler(_("Es sind noch {n} Stellen in eckigen Klammern offen. Bitte ausfüllen oder entfernen.",
                        n=offen), text)
    meta_schreiben(db, "datenschutz.text", inhalt[:50000])
    speichern(db, privacy_policy_url=str(request.base_url).rstrip("/") + "/datenschutz")
    db.commit()
    return _weiter("datenschutz")


@oeffentlich.get("/datenschutz", response_class=HTMLResponse)
def datenschutz_oeffentlich(request: Request, db: Session = Depends(get_db)):
    from app.datenschutz import html

    text = meta_lesen(db, "datenschutz.text")
    if not text:
        raise errors.not_found()
    return _seite(request, "datenschutz.html", None, db, inhalt=html(text))


# -- Sicherung
@router.get("/assistent/sicherung", response_class=HTMLResponse)
def sicherung_schritt(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app import sicherung

    return _schritt(request, user, db, "sicherung", einst=sicherung.einstellungen(db), letzte=sicherung.liste()[:1])


@router.post("/assistent/sicherung/jetzt", dependencies=[Depends(csrf_pruefen)])
def sicherung_jetzt(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app import sicherung

    e = sicherung.einstellungen(db)
    sicherung.erstellen("manuell", e["ordner"], e["tage"])
    return RedirectResponse("/verwaltung/assistent/sicherung", status_code=303)


# -- Spielleitung
@router.get("/assistent/spielleitung", response_class=HTMLResponse)
def spielleitung(request: Request, angelegt: str = "", user: User = Depends(verwalter),
                 db: Session = Depends(get_db)):
    from app.konto import registrierung

    return _schritt(request, user, db, "spielleitung", angelegt=angelegt, registrierung=registrierung(db))


@router.post("/assistent/spielleitung", dependencies=[Depends(csrf_pruefen)])
def spielleitung_anlegen(request: Request, username: str = Form(""), display_name: str = Form(""),
                         password: str = Form(""), user: User = Depends(verwalter), db: Session = Depends(get_db)):
    from app.cli import _neues_konto
    from app.konto import registrierung

    _ = tr(request)

    def fehler(t):
        return _fehler(request, user, db, "spielleitung", t, angelegt="", registrierung=registrierung(db))

    name = username.strip().lower()
    if not name or " " in name or len(name) > 64:
        return fehler(_("Benutzername fehlt oder enthält Leerzeichen."))
    if not display_name.strip():
        return fehler(_("Bitte einen Anzeigenamen angeben."))
    if (text := _passwort_fehler(_, name, password, password)):
        return fehler(text)
    if db.scalar(select(User).where(User.username == name)):
        return fehler(_("Den Benutzer „{name}“ gibt es schon.", name=name))
    _neues_konto(db, name, display_name.strip()[:128], password)
    db.commit()
    return RedirectResponse(f"/verwaltung/assistent/spielleitung?angelegt={name}", status_code=303)


# -- Fertig
@router.get("/assistent/fertig", response_class=HTMLResponse)
def fertig(request: Request, user: User = Depends(verwalter), db: Session = Depends(get_db)):
    return _schritt(request, user, db, "fertig", punkte=einrichtung.stand(db))


@router.post("/assistent/ausblenden", dependencies=[Depends(csrf_pruefen)])
def ausblenden(user: User = Depends(verwalter), db: Session = Depends(get_db)):
    meta_schreiben(db, "assistent.ausgeblendet", utcnow().isoformat())
    db.commit()
    return RedirectResponse("/verwaltung/", status_code=303)
