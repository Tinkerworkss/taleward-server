"""Anmelden mit Google, Discord, Apple, Microsoft (Schnittstelle 0.4.0).

Ablauf (die App spricht nie selbst mit den Diensten):
1. App → Systembrowser: GET /api/v1/auth/oidc/{dienst}/start?challenge=…&purpose=login|link[&linkToken=…]
2. Server → Dienst (Autorisierung). Der Dienst schickt den Browser zurück an /auth/oidc/{dienst}/callback.
3. Server tauscht den Code beim Dienst, prüft die Antwort (id_token: Signatur, Aussteller, Empfänger, Nonce, Ablauf;
   Discord: Profil über die API) und leitet auf taleward://auth?ticket=…&serverId=… weiter.
4. App → POST /api/v1/auth/oidc/exchange {ticket, verifier}. Nur wer den verifier zur challenge kennt, löst ein.

Jeder Server meldet sich selbst bei den Diensten an (Verwaltung → Anmeldung). Gespeichert wird je Konto und Dienst nur
die Kennung beim Dienst (sub, in AuthMethod kind „oidc:<dienst>“) – keine Tokens des Dienstes.
E-Mail-Adressen der Dienste gelten nur, wenn der Dienst sie als bestätigt meldet.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy.orm import Session

from app import einmal
from app.einstellungen import meta_lesen

DIENSTE = {
    "google": {
        "name": "Google", "art": "oidc", "scope": "openid email profile",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "jwks": "https://www.googleapis.com/oauth2/v3/certs",
        "aussteller": ("https://accounts.google.com", "accounts.google.com"),
        "extra": {"prompt": "select_account"},
    },
    "microsoft": {
        "name": "Microsoft", "art": "oidc", "scope": "openid email profile",
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "jwks": "https://login.microsoftonline.com/common/discovery/v2.0/keys",
        "aussteller": (),  # je Mandant: https://login.microsoftonline.com/<tid>/v2.0
        "extra": {"prompt": "select_account"},
    },
    "apple": {
        "name": "Apple", "art": "oidc", "scope": "name email",
        "auth": "https://appleid.apple.com/auth/authorize",
        "token": "https://appleid.apple.com/auth/token",
        "jwks": "https://appleid.apple.com/auth/keys",
        "aussteller": ("https://appleid.apple.com",),
        "extra": {"response_mode": "form_post"},  # Pflicht, sobald name/email angefragt werden
    },
    "discord": {
        "name": "Discord", "art": "oauth2", "scope": "identify email",
        "auth": "https://discord.com/oauth2/authorize",
        "token": "https://discord.com/api/oauth2/token",
        "profil": "https://discord.com/api/users/@me",
        "extra": {"prompt": "consent"},
    },
}
REIHENFOLGE = ("google", "discord", "apple", "microsoft")
MS_PRIVAT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # Mandant der privaten Microsoft-Konten
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
ZUSTAND_GUELTIG = timedelta(minutes=10)
TICKET_GUELTIG = timedelta(minutes=2)
REGISTRIERUNG_GUELTIG = timedelta(minutes=15)
VERBINDEN_GUELTIG = timedelta(minutes=5)


class DienstFehler(Exception):
    """code → taleward://auth?error=<code>"""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code


@dataclass
class Konfig:
    dienst: str
    client_id: str
    client_secret: str
    team_id: str = ""
    key_id: str = ""
    private_key: str = ""

    @property
    def eingerichtet(self) -> bool:
        if not self.client_id:
            return False
        if self.dienst == "apple":
            return bool(self.team_id and self.key_id and self.private_key)
        return bool(self.client_secret)


@dataclass
class Identitaet:
    dienst: str
    sub: str
    email: str | None  # nur bestätigte Adressen
    name: str | None


def konfig(db: Session, dienst: str) -> Konfig:
    def m(k: str) -> str:
        return meta_lesen(db, f"oidc.{dienst}.{k}") or ""

    return Konfig(dienst, m("client_id"), m("client_secret"), m("team_id"), m("key_id"), m("private_key"))


def eingerichtet(db: Session) -> list[str]:
    return [d for d in REIHENFOLGE if konfig(db, d).eingerichtet]


def rueckleitung(basis: str, dienst: str) -> str:
    return f"{basis.rstrip('/')}/auth/oidc/{dienst}/callback"


def challenge_von(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


# ---------------------------------------------------------------- 1. Start
def start(db: Session, dienst: str, challenge: str, purpose: str, link_token: str | None, basis: str,
          rueckweg: str = "taleward://auth") -> str:
    """URL beim Dienst. Fehler als DienstFehler (→ Rückweg in die App)."""
    if dienst not in DIENSTE or not konfig(db, dienst).eingerichtet:
        raise DienstFehler("provider_unknown")
    if not CHALLENGE.fullmatch(challenge or "") or purpose not in ("login", "link"):
        raise DienstFehler("oidc_failed", "challenge oder purpose ungültig")
    user_id = None
    if purpose == "link":
        gefunden = einmal.einloesen(db, "oidc_verbinden", link_token or "")
        if gefunden is None or gefunden[1].get("dienst") != dienst:
            raise DienstFehler("link_invalid")
        user_id = gefunden[0].user_id
    nonce = secrets.token_urlsafe(16)
    zustand = einmal.erzeugen(db, "oidc_zustand", ZUSTAND_GUELTIG, user_id=user_id, dienst=dienst,
                              challenge=challenge, purpose=purpose, nonce=nonce, rueckweg=rueckweg)
    db.commit()
    d, k = DIENSTE[dienst], konfig(db, dienst)
    werte = {"client_id": k.client_id, "redirect_uri": rueckleitung(basis, dienst), "response_type": "code",
             "scope": d["scope"], "state": zustand, **d.get("extra", {})}
    if d["art"] == "oidc":
        werte["nonce"] = nonce
    return f"{d['auth']}?{urlencode(werte)}"


# ---------------------------------------------------------------- 2. Rückkehr vom Dienst
def _apple_secret(k: Konfig) -> str:
    """Apple verlangt statt eines festen Schlüssels ein kurz gültiges, mit dem privaten Schlüssel signiertes JWT."""
    jetzt = int(time.time())
    return jwt.encode({"iss": k.team_id, "iat": jetzt, "exp": jetzt + 300, "aud": "https://appleid.apple.com",
                       "sub": k.client_id}, k.private_key, algorithm="ES256", headers={"kid": k.key_id})


def _id_token_pruefen(klient: httpx.Client, dienst: str, k: Konfig, token: str, nonce: str) -> dict:
    d = DIENSTE[dienst]
    try:
        kopf = jwt.get_unverified_header(token)
        schluessel = next((s for s in klient.get(d["jwks"]).json().get("keys", []) if s.get("kid") == kopf.get("kid")),
                          None)
        if schluessel is None:
            raise DienstFehler("oidc_failed", "Signaturschlüssel des Dienstes unbekannt")
        daten = jwt.decode(token, jwt.PyJWK(schluessel).key, algorithms=["RS256", "ES256"], audience=k.client_id,
                           leeway=60, options={"require": ["exp", "iat", "sub", "iss"]})
    except (jwt.PyJWTError, httpx.HTTPError, ValueError) as e:
        raise DienstFehler("oidc_failed", f"id_token ungültig: {type(e).__name__}") from None
    iss = daten.get("iss")
    if dienst == "microsoft":
        tid = daten.get("tid", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", tid) or iss != f"https://login.microsoftonline.com/{tid}/v2.0":
            raise DienstFehler("oidc_failed", "Aussteller passt nicht")
    elif iss not in d["aussteller"]:
        raise DienstFehler("oidc_failed", "Aussteller passt nicht")
    if daten.get("nonce") != nonce:
        raise DienstFehler("oidc_failed", "nonce passt nicht")
    return daten


def _bestaetigt(wert) -> bool:
    return wert is True or str(wert).lower() == "true"


def identitaet_holen(db: Session, dienst: str, code: str, basis: str, nonce: str, apple_user: str | None,
                     klient: httpx.Client) -> Identitaet:
    d, k = DIENSTE[dienst], konfig(db, dienst)
    geheim = _apple_secret(k) if dienst == "apple" else k.client_secret
    try:
        r = klient.post(d["token"], data={"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": rueckleitung(basis, dienst), "client_id": k.client_id,
                                          "client_secret": geheim}, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise DienstFehler("oidc_failed", f"Dienst nicht erreichbar ({type(e).__name__})") from None
    if r.status_code != 200:
        raise DienstFehler("oidc_failed", f"Code-Tausch abgelehnt ({r.status_code})")
    antwort = r.json()
    if d["art"] == "oauth2":  # Discord: Profil über die API
        try:
            p = klient.get(d["profil"], headers={"Authorization": f"Bearer {antwort.get('access_token', '')}"})
        except httpx.HTTPError as e:
            raise DienstFehler("oidc_failed", f"Profil nicht lesbar ({type(e).__name__})") from None
        if p.status_code != 200 or not str(p.json().get("id", "")):
            raise DienstFehler("oidc_failed", "Profil nicht lesbar")
        pr = p.json()
        email = pr.get("email") if pr.get("verified") is True else None
        return Identitaet(dienst, str(pr["id"]), email, pr.get("global_name") or pr.get("username"))
    daten = _id_token_pruefen(klient, dienst, k, antwort.get("id_token", ""), nonce)
    email = daten.get("email")
    if dienst == "microsoft":
        # Nur private Microsoft-Konten melden geprüfte Adressen; bei Firmenkonten kann jeder Mandant sie frei setzen
        bestaetigt = daten.get("tid") == MS_PRIVAT
    else:
        bestaetigt = _bestaetigt(daten.get("email_verified"))
    name = daten.get("name")
    if dienst == "apple" and apple_user:  # Apple schickt den Namen nur beim allerersten Mal, im Formular
        try:
            n = json.loads(apple_user).get("name") or {}
            name = " ".join(x for x in (n.get("firstName"), n.get("lastName")) if x) or None
        except (ValueError, AttributeError):
            pass
    return Identitaet(dienst, str(daten["sub"]), email.strip().lower() if email and bestaetigt else None, name)


def rueckkehr(db: Session, dienst: str, werte: dict, basis: str, klient: httpx.Client | None = None) -> str:
    """Antwort des Dienstes verarbeiten → Adresse taleward://auth?… für den Browser."""
    from app.db import server_id

    zustand = einmal.einloesen(db, "oidc_zustand", werte.get("state") or "")
    db.commit()
    if zustand is None or zustand[1].get("dienst") != dienst:
        return "taleward://auth?" + urlencode({"error": "oidc_failed"})
    z = zustand[1]
    weg = z.get("rueckweg") or "taleward://auth"  # Web-Fassung: https://…/app/#/auth
    if werte.get("error"):
        code = "cancelled" if werte["error"] in ("access_denied", "user_cancelled_authorize") else "oidc_failed"
        return f"{weg}?" + urlencode({"error": code})
    eigener = klient is None
    klient = klient or httpx.Client(timeout=20)
    try:
        ident = identitaet_holen(db, dienst, werte.get("code") or "", basis, z["nonce"], werte.get("user"), klient)
    except DienstFehler as e:
        return f"{weg}?" + urlencode({"error": e.code})
    finally:
        if eigener:
            klient.close()
    ticket = einmal.erzeugen(db, "oidc_ticket", TICKET_GUELTIG, user_id=zustand[0].user_id, dienst=dienst,
                             sub=ident.sub, email=ident.email, name=ident.name, purpose=z["purpose"],
                             challenge=z["challenge"])
    db.commit()
    return f"{weg}?" + urlencode({"ticket": ticket, "serverId": server_id()})
