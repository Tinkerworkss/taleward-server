"""Wie lange Aufnahmen auf dem Server bleiben (Schnittstelle 0.4.4, ServerInfo.audioRetention).

- until_release (Standard für neue Server): Die Aufnahme bleibt nach der Transkription, damit die Zusammenfassung
  gegen sie geprüft werden kann, bis die Spielleitung den Recap freigibt – höchstens maxDays Tage.
- immediate: Die Aufnahme wird gelöscht, sobald sie in Text umgewandelt ist (Verhalten bis Server 0.4.10). Scheitert
  die Transkription, bleibt sie für „Erneut versuchen“ höchstens AUDIO_RETENTION_DAYS Tage.

Server, die schon Aufnahmen oder Zustimmungen haben, starten nach dem Update mit immediate – ihre Mitglieder haben
„wird danach gelöscht“ zugestimmt. Wird die Aufbewahrung verlängert, gelten die bisherigen Zustimmungen nicht mehr:
Der Server setzt sie zurück (mit Eintrag im Zustimmungsprotokoll), die App fragt dann neu.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.db import utcnow
from app.einstellungen import meta_lesen, meta_schreiben
from app.models import ConsentLog, GameSession, Job, Member, Upload

MODI = ("until_release", "immediate")
HOECHSTENS_TAGE = 7
K_MODUS, K_TAGE = "audio.aufbewahrung", "audio.max_tage"


@dataclass(frozen=True)
class Aufbewahrung:
    modus: str
    tage: int

    @property
    def bis_freigabe(self) -> bool:
        return self.modus == "until_release"

    def api(self) -> dict:
        return {"mode": self.modus, "maxDays": self.tage if self.bis_freigabe else None}

    def laenger_als(self, alt: "Aufbewahrung") -> bool:
        """Braucht diese Einstellung eine neue Zustimmung gegenüber der alten?"""
        if not self.bis_freigabe:
            return False
        return not alt.bis_freigabe or self.tage > alt.tage


def lesen(db: Session) -> Aufbewahrung:
    modus = meta_lesen(db, K_MODUS) or "immediate"  # ohne Eintrag (vor festlegen): das Vorsichtigere
    try:
        tage = int(meta_lesen(db, K_TAGE) or HOECHSTENS_TAGE)
    except ValueError:
        tage = HOECHSTENS_TAGE
    return Aufbewahrung(modus if modus in MODI else "immediate", min(max(tage, 1), HOECHSTENS_TAGE))


def festlegen(db: Session) -> None:
    """Beim Start: Standard einmalig festschreiben – neue Server until_release, bestehende immediate."""
    if meta_lesen(db, K_MODUS) is not None:
        return
    bestehend = (db.scalar(select(GameSession.id).limit(1)) is not None
                 or db.scalar(select(Member.id).where(Member.recording_consent_at.is_not(None)).limit(1)) is not None)
    meta_schreiben(db, K_MODUS, "immediate" if bestehend else "until_release")
    meta_schreiben(db, K_TAGE, str(HOECHSTENS_TAGE))
    db.commit()


def speichern(db: Session, modus: str, tage: int, von_user_id: str | None) -> int:
    """Einstellung ändern. Gibt zurück, wie viele Zustimmungen zurückgesetzt wurden."""
    if modus not in MODI or not 1 <= tage <= HOECHSTENS_TAGE:
        raise ValueError(modus)
    alt = lesen(db)
    neu = Aufbewahrung(modus, tage)
    meta_schreiben(db, K_MODUS, modus)
    meta_schreiben(db, K_TAGE, str(tage))
    zurueckgesetzt = 0
    if neu.laenger_als(alt):
        now = utcnow()
        for m in db.scalars(select(Member).where(Member.recording_consent_at.is_not(None))):
            m.recording_consent_at = None
            db.add(ConsentLog(campaign_id=m.campaign_id, member_id=m.id, user_id=m.user_id, action="reset",
                              recorded_by_user_id=von_user_id, at=now))
            zurueckgesetzt += 1
    db.flush()
    if not neu.bis_freigabe:
        nach_transkription_loeschen(db)
    return zurueckgesetzt


def audio_loeschen(db: Session, s: GameSession) -> bool:
    """Alle Aufnahmen einer Session löschen (Hörproben bleiben – die gehören zur Stimmzuordnung)."""
    for up in db.scalars(select(Upload).where(Upload.session_id == s.id)):
        storage.delete_upload_files(up.id)
    if s.audio_deleted_at is None:
        s.audio_deleted_at = utcnow()
        return True
    return False


def nach_transkription_loeschen(db: Session) -> int:
    """immediate: Aufnahmen löschen, deren Transkription schon fertig ist (z. B. nach dem Umstellen)."""
    fertig = select(Job.session_id).where(Job.type == "transcribe", Job.state == "done")
    zahl = 0
    for s in db.scalars(select(GameSession).where(GameSession.audio_deleted_at.is_(None), GameSession.id.in_(fertig),
                                                  GameSession.state != "uploading")):
        # Nur, wenn der LETZTE Transkriptionsauftrag fertig ist – nach neuem Hochladen gehört das Audio zum neuen
        letzter = db.scalar(select(Job).where(Job.session_id == s.id, Job.type == "transcribe")
                            .order_by(Job.created_at.desc()).limit(1))
        if letzter is not None and letzter.state == "done":
            zahl += audio_loeschen(db, s)
    return zahl


def abgelaufene_loeschen(db: Session, standard_tage: int) -> int:
    """Wartung: Aufnahmen spätestens nach der Frist löschen – bis_freigabe: maxDays, sonst standard_tage
    (nur fehlgeschlagene Transkriptionen haben dann noch Audio)."""
    a = lesen(db)
    zahl = nach_transkription_loeschen(db) if not a.bis_freigabe else 0
    grenze = utcnow() - timedelta(days=a.tage if a.bis_freigabe else standard_tage)
    for up in db.scalars(select(Upload).where(Upload.state == "completed", Upload.completed_at < grenze)):
        s = db.get(GameSession, up.session_id)
        if s is not None and s.audio_deleted_at is None:
            zahl += audio_loeschen(db, s)
    return zahl
