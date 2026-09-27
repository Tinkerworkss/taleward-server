"""Passwort-Link: Die Verwaltung erzeugt für ein Konto einen einmaligen Link (24 Stunden gültig). Wer ihn öffnet,
setzt ein neues Passwort; danach ist das Konto überall abgemeldet. So muss der Verwalter kein Passwort ausdenken und
weitergeben. Ein neuer Link ersetzt den alten.

Gespeichert wird nur der SHA-256-Wert des Geheimnisses (server_meta pwlink.<userId> = "<hash>|<bis>").
Link-Form: /passwort/<userId>.<geheimnis>
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben
from app.models import AuthMethod, User
from app.security import hash_password

GUELTIG = timedelta(hours=24)


def _hash(geheim: str) -> str:
    return hashlib.sha256(geheim.encode()).hexdigest()


def erzeugen(db: Session, user: User, gueltig: timedelta = GUELTIG) -> tuple[str, datetime]:
    geheim = secrets.token_urlsafe(24)
    bis = utcnow() + gueltig
    meta_schreiben(db, f"pwlink.{user.id}", f"{_hash(geheim)}|{bis.isoformat()}")
    db.commit()
    return f"{user.id}.{geheim}", bis


def pruefen(db: Session, token: str) -> User | None:
    uid, _, geheim = (token or "").partition(".")
    if not uid or not geheim or len(token) > 200:
        return None
    wert = meta_lesen(db, f"pwlink.{uid}") or ""
    soll, _, bis = wert.partition("|")
    if not soll or not bis:
        return None
    ende = datetime.fromisoformat(bis)
    if ende.tzinfo is None:
        ende = ende.replace(tzinfo=timezone.utc)
    if ende <= utcnow() or not hmac.compare_digest(soll, _hash(geheim)):
        return None
    return db.get(User, uid)


def passwort_setzen(db: Session, user: User, passwort: str) -> None:
    """Neues Passwort und überall abmelden (App und Verwaltung). Ein offener Passwort-Link verfällt."""
    methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == user.id, AuthMethod.kind == "password"))
    if methode is None:
        db.add(AuthMethod(user_id=user.id, kind="password", secret=hash_password(passwort)))
    else:
        methode.secret = hash_password(passwort)
    user.token_version += 1
    user.token_ausnahme = None
    if meta_lesen(db, f"pwlink.{user.id}"):
        meta_schreiben(db, f"pwlink.{user.id}", "")


def einloesen(db: Session, token: str, passwort: str) -> User | None:
    user = pruefen(db, token)
    if user is None:
        return None
    passwort_setzen(db, user, passwort)
    db.commit()
    return user
