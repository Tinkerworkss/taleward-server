"""Schnittstelle 0.4.0: E-Mail am Konto, „Passwort vergessen“, Passwort ändern, Anmelden mit Diensten.

API (unter /api/v1): /auth/password-reset, /auth/oidc/{dienst}/start, /auth/oidc/exchange, /me/email, /me/password,
/me/providers/…. Webseiten ohne /api/v1 (im Browser): /auth/oidc/{dienst}/callback, /konto/email/<token>.
"""
from __future__ import annotations

import hmac
import logging
import re
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import anmeldedienste, einmal, errors, mail, schemas
from app.access import current_user
from app.db import get_db, server_id, utcnow
from app.einstellungen import mail_adresse, oeffentliche_adresse
from app.models import AuthMethod, User
from app.security import create_token, token_jti, verify_password
from app.services import user_out

log = logging.getLogger("anmeldung")
router = APIRouter(tags=["Auth"])  # unter /api/v1
seiten = APIRouter(include_in_schema=False)  # Webseiten und Rückleitung der Dienste

DIENST_MUSTER = "^(google|discord|apple|microsoft)$"
EMAIL_GUELTIG = timedelta(hours=24)
RESET_GUELTIG = timedelta(hours=1)

# ---------------------------------------------------------------- Begrenzung
def _zu_viele(schluessel: str, anzahl: int, fenster: float) -> bool:
    from app.begrenzung import ZAEHLER

    return not ZAEHLER.versuch(schluessel, anzahl, fenster)


def versuche_vergessen() -> None:
    from app.begrenzung import ZAEHLER

    ZAEHLER.vergessen()


def _adresse(request: Request) -> str:
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------- Passwort vergessen
def _reset_senden(user_id: str, sprache: str, basis: str) -> None:
    """Im Hintergrund, damit die Antwortzeit nicht verrät, ob es das Konto gibt."""
    from app import passwortlink
    from app.db import session_factory

    with session_factory()() as db:
        u = db.get(User, user_id)
        if u is None or not u.email:
            return
        token, _ = passwortlink.erzeugen(db, u, RESET_GUELTIG)
        betreff, text = mail.text(db, "passwort", sprache, name=u.display_name, benutzer=u.username,
                                  link=f"{basis}/passwort/{token}")
        try:
            mail.senden(db, u.email, betreff, text)
        except mail.MailFehler:
            pass


@router.post("/auth/password-reset", status_code=202)
def passwort_vergessen(body: schemas.PasswordResetRequest, request: Request, hintergrund: BackgroundTasks,
                       db: Session = Depends(get_db)):
    if _zu_viele(f"reset:{_adresse(request)}", 10, 15 * 60):
        raise errors.ApiError(429, "too_many_requests")
    login = (body.login or "").strip().lower()
    basis = mail_adresse(db)
    if login and basis and mail.kann_senden(db):
        feld = User.email if "@" in login else User.username
        u = db.scalar(select(User).where(feld == login))
        if u is not None and u.email and not u.setup_account and not _zu_viele(f"reset-konto:{u.id}", 3, 3600):
            hintergrund.add_task(_reset_senden, u.id, errors.sprache(request), basis)
    return Response(status_code=202)


# ---------------------------------------------------------------- E-Mail
@router.put("/me/email", status_code=202)
def email_eintragen(body: schemas.EmailRequest, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    adresse = mail.normal(body.email)
    if not mail.gueltig(adresse):
        raise errors.bad_request("invalid_email")
    anderer = db.scalar(select(User).where(User.email == adresse, User.id != user.id))
    if anderer is not None:
        raise errors.conflict("email_taken")
    basis = mail_adresse(db)
    if not mail.kann_senden(db) or not basis:
        raise errors.ApiError(503, "mail_unavailable")
    if _zu_viele(f"email:{user.id}", 5, 3600):
        raise errors.ApiError(429, "too_many_requests")
    einmal.verwerfen(db, "email_bestaetigen", user.id)
    wert = einmal.erzeugen(db, "email_bestaetigen", EMAIL_GUELTIG, user_id=user.id, email=adresse)
    user.email_pending = adresse
    betreff, text = mail.text(db, "bestaetigen", errors.sprache(request), name=user.display_name,
                              benutzer=user.username, link=f"{basis}/konto/email/{wert}")
    try:
        mail.senden(db, adresse, betreff, text)
    except mail.MailFehler:
        db.rollback()
        raise errors.ApiError(503, "mail_unavailable") from None
    db.commit()
    return Response(status_code=202)


@router.delete("/me/email", status_code=204)
def email_entfernen(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.email = user.email_pending = user.email_verified_at = None
    einmal.verwerfen(db, "email_bestaetigen", user.id)
    db.commit()
    return Response(status_code=204)


def _seite(request: Request, db: Session, **ctx) -> HTMLResponse:
    from app.verwaltung.router import _seite as seite

    return seite(request, "konto_email.html", None, db, **ctx)


@seiten.get("/konto/email/{token}", response_class=HTMLResponse)
def email_seite(token: str, request: Request, db: Session = Depends(get_db)):
    """Zeigt nur an – bestätigt wird erst mit dem Knopf (Mailprogramme öffnen Links manchmal von selbst)."""
    t = einmal.ansehen(db, "email_bestaetigen", token)
    u = db.get(User, t.user_id) if t else None
    if u is None:
        antwort = _seite(request, db, gueltig=False, fertig=False, token="", adresse="", konto="", fehler="")
        antwort.status_code = 404
        return antwort
    import json

    return _seite(request, db, gueltig=True, fertig=False, token=token, adresse=json.loads(t.daten)["email"],
                  konto=u.username, fehler="")


@seiten.post("/konto/email/{token}", response_class=HTMLResponse)
def email_bestaetigen(token: str, request: Request, db: Session = Depends(get_db)):
    from app.verwaltung.router import tr

    gefunden = einmal.einloesen(db, "email_bestaetigen", token)
    u = db.get(User, gefunden[0].user_id) if gefunden else None
    if u is None:
        db.rollback()
        return email_seite(token, request, db)
    adresse = gefunden[1]["email"]
    if db.scalar(select(User).where(User.email == adresse, User.id != u.id)) is not None:
        db.commit()
        antwort = _seite(request, db, gueltig=True, fertig=False, token="", adresse=adresse, konto=u.username,
                         fehler=tr(request)("Diese Adresse gehört inzwischen zu einem anderen Konto."))
        antwort.status_code = 409
        return antwort
    u.email, u.email_verified_at = adresse, utcnow()
    if u.email_pending == adresse:
        u.email_pending = None
    db.commit()
    return _seite(request, db, gueltig=True, fertig=True, token="", adresse=adresse, konto=u.username, fehler="")


# ---------------------------------------------------------------- Konto löschen ohne App (Seite)
@seiten.get("/konto-loeschen", response_class=HTMLResponse)
def konto_loeschen_seite(request: Request, db: Session = Depends(get_db)):
    """Öffentliche Seite, die der Store-Eintrag verlangt: Konto ohne App löschen, mit Benutzername und Passwort.
    Bestätigungslogik wie DELETE /me (app/konto.py); Fehlversuche zählen wie beim Anmelden."""
    from app.verwaltung.router import _seite as seite

    return seite(request, "konto_loeschen.html", None, db, fertig=False, konto="", fehler="")


@seiten.post("/konto-loeschen", response_class=HTMLResponse)
def konto_loeschen_ausfuehren(request: Request, username: str = Form(""), password: str = Form(""),
                              bestaetigt: str = Form(""), db: Session = Depends(get_db)):
    from app.begrenzung import ZAEHLER
    from app.konto import entfernen
    from app.routers.auth import LOGIN_FENSTER, LOGIN_JE_ADRESSE, LOGIN_JE_NAME
    from app.verwaltung.router import _seite as seite, sprache_von, tr

    _ = tr(request)
    name = username.strip().lower()[:64]
    adresse = request.client.host if request.client else "?"

    def fehler(text: str, status: int = 400) -> HTMLResponse:
        db.rollback()
        antwort = seite(request, "konto_loeschen.html", None, db, fertig=False, konto=name, fehler=text)
        antwort.status_code = status
        return antwort

    if not bestaetigt:
        return fehler(_("Bitte das Löschen mit dem Haken bestätigen."))
    if ZAEHLER.voll(f"login-adr:{adresse}", LOGIN_JE_ADRESSE, LOGIN_FENSTER) or \
            ZAEHLER.voll(f"login-name:{name}", LOGIN_JE_NAME, LOGIN_FENSTER):
        return fehler(_("Zu viele Versuche. Bitte in einer Viertelstunde noch einmal."), 429)
    user = db.scalar(select(User).where(User.username == name)) if name else None
    methode = None
    if user is not None:
        methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id, AuthMethod.kind == "password"))
    if user is not None and methode is None and not user.setup_account:
        return fehler(_("Dieses Konto hat kein Passwort (Anmeldung über einen Dienst). Bitte in der App unter Konto löschen."))
    if not verify_password(password, methode.secret if methode else None) or user.setup_account:
        ZAEHLER.zaehlen(f"login-adr:{adresse}", LOGIN_FENSTER)
        ZAEHLER.zaehlen(f"login-name:{name}", LOGIN_FENSTER)
        return fehler(_("Benutzername oder Passwort stimmen nicht."), 401)
    try:
        entfernen(db, user)
    except errors.ApiError as e:
        return fehler(e.message(sprache_von(request)), 409)
    db.commit()
    return seite(request, "konto_loeschen.html", None, db, fertig=True, konto=name, fehler="")


# ---------------------------------------------------------------- Passwort setzen oder ändern
@router.put("/me/password", status_code=204)
def passwort_aendern(body: schemas.PasswordChangeRequest, request: Request, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    from app import passwortlink
    from app.konto import passwort_zu_schwach

    methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id, AuthMethod.kind == "password"))
    if methode is not None and not verify_password(body.current_password or "", methode.secret):
        raise errors.ApiError(401, "wrong_password")
    if passwort_zu_schwach(body.new_password or "", user.username, user.display_name):
        raise errors.bad_request("weak_password")
    alt = user.token_version
    passwortlink.passwort_setzen(db, user, body.new_password)
    # Alle anderen Geräte abgemeldet, dieses bleibt angemeldet
    auth = request.headers.get("authorization", "")
    user.token_ausnahme = f"{alt}:{token_jti(auth.split(' ', 1)[-1])}"
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- Dienste verbinden und trennen
def _pruef_dienst(db: Session, dienst: str) -> None:
    if dienst not in anmeldedienste.DIENSTE or not anmeldedienste.konfig(db, dienst).eingerichtet:
        raise errors.ApiError(404, "provider_unknown")


@router.post("/me/providers/{provider}/link", response_model=schemas.LinkTokenOut)
def verbinden_starten(provider: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _pruef_dienst(db, provider)
    wert = einmal.erzeugen(db, "oidc_verbinden", anmeldedienste.VERBINDEN_GUELTIG, user_id=user.id, dienst=provider)
    db.commit()
    return schemas.LinkTokenOut(link_token=wert)


@router.delete("/me/providers/{provider}", status_code=204)
def trennen(provider: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if provider not in anmeldedienste.DIENSTE:
        raise errors.ApiError(404, "provider_unknown")
    arten = {a.kind: a for a in db.scalars(select(AuthMethod).where(AuthMethod.user_id == user.id))}
    methode = arten.get(f"oidc:{provider}")
    if methode is None:
        raise errors.ApiError(404, "provider_unknown")
    if len(arten) <= 1:
        raise errors.conflict("last_login_method")
    db.delete(methode)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- Anmelden mit Dienst
@router.get("/auth/oidc/{provider}/start")
def dienst_start(provider: str, request: Request, challenge: str = "", purpose: str = "login",
                 linkToken: str | None = None, returnTo: str | None = None,  # noqa: N803 – Namen aus der YAML
                 db: Session = Depends(get_db)):
    from app import webapp

    if _zu_viele(f"oidc-start:{_adresse(request)}", 30, 15 * 60):  # jeder Aufruf legt einen Datensatz an
        return RedirectResponse("taleward://auth?error=too_many_requests", status_code=302)
    basis = oeffentliche_adresse(db, request)
    rueckweg = "taleward://auth"
    if returnTo:
        if returnTo not in webapp.rueckwege(db, basis):  # nur fest hinterlegte Ziele (kein offener Umleiter)
            return RedirectResponse("taleward://auth?error=return_to_not_allowed", status_code=302)
        rueckweg = returnTo
    try:
        ziel = anmeldedienste.start(db, provider, challenge, purpose, linkToken, basis, rueckweg)
    except anmeldedienste.DienstFehler as e:
        db.rollback()
        return RedirectResponse(webapp.mit_parametern(rueckweg, f"error={e.code}"), status_code=302)
    return RedirectResponse(ziel, status_code=302)


def _zur_app(ziel: str, status: int = 303) -> RedirectResponse:
    """Weiterleitung in die App; der Text dient nur Browsern, die Weiterleitungen auf taleward:// nicht ausführen."""
    from html import escape

    antwort = RedirectResponse(ziel, status_code=status)
    antwort.body = (f'<!doctype html><meta charset="utf-8"><title>Taleward</title>'
                    f'<p><a href="{escape(ziel)}">Zurück zu Taleward</a></p>').encode()
    antwort.headers["content-type"] = "text/html; charset=utf-8"
    antwort.headers["content-length"] = str(len(antwort.body))
    antwort.headers["Cache-Control"] = "no-store"
    antwort.headers["Referrer-Policy"] = "no-referrer"
    return antwort


@seiten.get("/auth/oidc/{provider}/callback")
def dienst_rueckkehr(provider: str, request: Request, db: Session = Depends(get_db)):
    return _zur_app(anmeldedienste.rueckkehr(db, provider, dict(request.query_params),
                                             oeffentliche_adresse(db, request)))


@seiten.post("/auth/oidc/{provider}/callback")
def dienst_rueckkehr_formular(provider: str, request: Request, code: str = Form(""), state: str = Form(""),
                              error: str = Form(""), user: str = Form(""), db: Session = Depends(get_db)):
    """Apple schickt die Antwort als Formular (response_mode=form_post)."""
    werte = {"code": code, "state": state, "error": error, "user": user}
    return _zur_app(anmeldedienste.rueckkehr(db, provider, {k: v for k, v in werte.items() if v},
                                             oeffentliche_adresse(db, request)))


@router.post("/auth/oidc/exchange", response_model=schemas.OidcExchangeResponse)
def dienst_tausch(body: schemas.OidcExchangeRequest, request: Request, db: Session = Depends(get_db)):
    antwort = _tausch(body, request, db)
    daten = antwort.model_dump(by_alias=True, mode="json")
    if daten.get("user") is None:
        daten.pop("user", None)  # YAML: user ist ein Objekt oder fehlt
    from fastapi.responses import JSONResponse

    return JSONResponse(daten)


def _tausch(body: schemas.OidcExchangeRequest, request: Request, db: Session) -> schemas.OidcExchangeResponse:
    if _zu_viele(f"tausch:{_adresse(request)}", 30, 15 * 60):
        raise errors.ApiError(429, "too_many_requests")
    t = einmal.ansehen(db, "oidc_ticket", body.ticket)
    import json

    daten = json.loads(t.daten) if t else {}
    if t is None or not hmac.compare_digest(anmeldedienste.challenge_von(body.verifier or ""),
                                            daten.get("challenge", "")):
        raise errors.bad_request("ticket_invalid")
    einmal.einloesen(db, "oidc_ticket", body.ticket)
    dienst, sub, email = daten["dienst"], daten["sub"], daten.get("email")
    kind = f"oidc:{dienst}"
    vorhanden = db.scalar(select(AuthMethod).where(AuthMethod.kind == kind, AuthMethod.secret == sub))

    if daten["purpose"] == "link":
        u = db.get(User, t.user_id) if t.user_id else None
        if u is None:
            raise errors.bad_request("ticket_invalid")
        if vorhanden is not None and vorhanden.user_id != u.id:
            db.rollback()
            raise errors.conflict("provider_linked_elsewhere")
        eigene = db.scalar(select(AuthMethod).where(AuthMethod.user_id == u.id, AuthMethod.kind == kind))
        if eigene is None:
            db.add(AuthMethod(user_id=u.id, kind=kind, secret=sub))
        else:
            eigene.secret = sub
        _email_uebernehmen(db, u, email)
        db.commit()
        db.refresh(u)
        return schemas.OidcExchangeResponse(status="ok", user=user_out(u), provider=dienst)

    if vorhanden is not None:
        u = db.get(User, vorhanden.user_id)
        vorhanden.last_used_at = utcnow()
        _email_uebernehmen(db, u, email)
        db.commit()
        token, bis = create_token(u.id, u.token_version, server_id())
        return schemas.OidcExchangeResponse(status="ok", access_token=token, expires_at=bis, user=user_out(u),
                                            provider=dienst)
    if email and db.scalar(select(User).where(User.email == email)) is not None:
        db.commit()
        return schemas.OidcExchangeResponse(status="email_in_use", email=email, provider=dienst)
    wert = einmal.erzeugen(db, "oidc_registrierung", anmeldedienste.REGISTRIERUNG_GUELTIG, dienst=dienst, sub=sub,
                           email=email, name=daten.get("name"))
    db.commit()
    return schemas.OidcExchangeResponse(status="register", registration_token=wert,
                                        suggested_display_name=(daten.get("name") or "")[:128] or None,
                                        email=email, provider=dienst)


def _email_uebernehmen(db: Session, u: User, email: str | None) -> None:
    """Hat das Konto noch keine bestätigte Adresse, übernimmt es die vom Dienst bestätigte (falls frei)."""
    if email and not u.email and db.scalar(select(func.count()).select_from(User).where(User.email == email)) == 0:
        u.email, u.email_verified_at = email, utcnow()


# ---------------------------------------------------------------- Registrierung mit Dienst (aus /auth/register)
def benutzername_bilden(db: Session, email: str | None, name: str | None) -> str:
    grund = (email.split("@", 1)[0] if email else "") or (name or "")
    grund = re.sub(r"[^a-z0-9._-]+", "", grund.lower().replace(" ", "."))[:30].strip("._-")
    if len(grund) < 3:
        grund = "spieler"
    kandidat, n = grund, 2
    while db.scalar(select(User).where(User.username == kandidat)) is not None:
        kandidat, n = f"{grund}{n}", n + 1
    return kandidat


def registrieren_mit_dienst(db: Session, adresse: str, body: schemas.RegisterRequest) -> User:
    from app.konto import registrieren

    t = einmal.ansehen(db, "oidc_registrierung", body.registration_token or "")
    if t is None:
        raise errors.bad_request("registration_token_invalid")
    import json

    daten = json.loads(t.daten)
    kind = f"oidc:{daten['dienst']}"
    if db.scalar(select(AuthMethod).where(AuthMethod.kind == kind, AuthMethod.secret == daten["sub"])) is not None:
        raise errors.bad_request("registration_token_invalid")
    email = daten.get("email")
    name = benutzername_bilden(db, email, daten.get("name"))
    u = registrieren(db, adresse, body.invite_code, name, body.display_name, None, body.accept_privacy,
                     body.age_confirmed, methode=(kind, daten["sub"]))
    einmal.einloesen(db, "oidc_registrierung", body.registration_token)
    if email and db.scalar(select(User).where(User.email == email, User.id != u.id)) is None:
        u.email, u.email_verified_at = email, utcnow()
    return u
