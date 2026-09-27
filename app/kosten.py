"""Kosten für Cloud-Dienste (Transkription, Sprachmodell) und das monatliche Limit des Betreibers.

Gezählt wird, was im Verbrauchsprotokoll als extern (Cloud) mit Kosten steht, im laufenden Kalendermonat (UTC).
Ist das Limit erreicht, bleiben neue Cloud-Aufträge liegen, bis der Monat wechselt oder das Limit steigt.
Eigene Worker und lokale Modelle kosten nichts und laufen immer weiter.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.einstellungen import meta_lesen
from app.models import UsageLog


def monatsbeginn() -> datetime:
    jetzt = datetime.now(timezone.utc)
    return jetzt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def monat_cent(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(UsageLog.cost_cents), 0)).where(
        UsageLog.engine == "external", UsageLog.created_at >= monatsbeginn())) or 0)


def limit_cent(db: Session) -> int | None:
    try:
        wert = int(meta_lesen(db, "kosten.limit_cent") or 0)
    except ValueError:
        return None
    return wert if wert > 0 else None


def erreicht(db: Session) -> bool:
    grenze = limit_cent(db)
    return grenze is not None and monat_cent(db) >= grenze
