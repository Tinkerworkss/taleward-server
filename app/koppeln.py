"""Worker koppeln per Code, statt einen langen Schlüssel von Hand in die .env zu kopieren.

Die Verwaltung erzeugt einen kurzen Code (15 Minuten gültig, nur einmal verwendbar). Der Worker schickt ihn einmal an
POST /worker/v1/pair und bekommt dafür seinen Zugangsschlüssel. Versuche sind je Adresse begrenzt.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben
from app.models import Worker

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GUELTIG = timedelta(minutes=15)
MAX_VERSUCHE, FENSTER = 10, 15 * 60
_versuche: dict[str, deque] = defaultdict(deque)
_sperre = threading.Lock()


def code_erzeugen(db: Session) -> tuple[str, datetime]:
    code = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(2))
    bis = utcnow() + GUELTIG
    meta_schreiben(db, "koppeln.code", code)
    meta_schreiben(db, "koppeln.bis", bis.isoformat())
    db.commit()
    return code, bis


def aktueller_code(db: Session) -> tuple[str, datetime] | None:
    code, bis = meta_lesen(db, "koppeln.code"), meta_lesen(db, "koppeln.bis")
    if not code or not bis:
        return None
    ende = datetime.fromisoformat(bis)
    if ende.tzinfo is None:
        ende = ende.replace(tzinfo=timezone.utc)
    return (code, ende) if ende > utcnow() else None


def _begrenzen(adresse: str) -> None:
    jetzt = time.monotonic()
    with _sperre:
        q = _versuche[adresse]
        while q and q[0] < jetzt - FENSTER:
            q.popleft()
        if len(q) >= MAX_VERSUCHE:
            raise errors.ApiError(429, "too_many_requests")
        q.append(jetzt)


def versuche_vergessen() -> None:
    with _sperre:
        _versuche.clear()


def koppeln(db: Session, adresse: str, code: str, name: str) -> tuple[Worker, str]:
    """Code einlösen → neuer Worker mit Schlüssel. Der Code ist danach verbraucht."""
    from app.routers.worker import token_hash

    _begrenzen(adresse)
    aktuell = aktueller_code(db)
    ist = (code or "").strip().upper().replace(" ", "").replace("-", "")
    if aktuell is None or not hmac.compare_digest(aktuell[0].replace("-", ""), ist):
        raise errors.ApiError(404, "pairing_code_invalid")
    basis = (name or "").strip()[:80] or "worker"
    vorhanden = set(db.scalars(select(Worker.name).where(Worker.revoked_at.is_(None))))
    name, n = basis, 2
    while name in vorhanden:
        name, n = f"{basis}-{n}", n + 1
    geheim = secrets.token_urlsafe(32)
    w = Worker(name=name, token_hash=token_hash(geheim), capabilities="asr,llm")
    db.add(w)
    meta_schreiben(db, "koppeln.code", "")
    db.commit()
    return w, f"wk.{w.id}.{geheim}"
