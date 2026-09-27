"""Einmal-Werte (0.4.0): gespeichert wird nur die SHA-256, eingelöst genau einmal, mit Ablauf."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import EinmalToken


def _hash(wert: str) -> str:
    return hashlib.sha256((wert or "").encode()).hexdigest()


def erzeugen(db: Session, art: str, gueltig: timedelta, user_id: str | None = None, **daten) -> str:
    wert = secrets.token_urlsafe(32)
    db.add(EinmalToken(id=_hash(wert), art=art, user_id=user_id, daten=json.dumps(daten),
                       expires_at=utcnow() + gueltig))
    db.flush()
    return wert


def ansehen(db: Session, art: str, wert: str) -> EinmalToken | None:
    if not wert or len(wert) > 200:
        return None
    t = db.get(EinmalToken, _hash(wert))
    if t is None or t.art != art or t.expires_at <= utcnow():
        return None
    return t


def einloesen(db: Session, art: str, wert: str) -> tuple[EinmalToken, dict] | None:
    """Gültigen Wert verbrauchen. None bei unbekannt, falscher Art oder abgelaufen."""
    t = ansehen(db, art, wert)
    if t is None:
        return None
    daten = json.loads(t.daten or "{}")
    db.delete(t)
    db.flush()
    return t, daten


def verwerfen(db: Session, art: str, user_id: str) -> None:
    db.execute(delete(EinmalToken).where(EinmalToken.art == art, EinmalToken.user_id == user_id))


def aufraeumen(db: Session) -> int:
    n = db.execute(delete(EinmalToken).where(EinmalToken.expires_at <= utcnow() - timedelta(minutes=1))).rowcount
    db.commit()
    return n or 0
