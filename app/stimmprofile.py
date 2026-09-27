"""Stimmprofile (Schritt 6) – in der Zentrale.

Pflichtregeln:
- Gespeichert wird nur der Stimmabdruck (Zahlenvektor), nie Audio. Die Aufnahme liegt nur so lange auf der Platte,
  bis ein Worker den Abdruck berechnet hat (bei Fehlschlag spätestens nach einem Tag, siehe Wartung).
- Löschen entfernt alles daraus Gelernte: Basis-Abdruck, gelernte Summe, Zähler, offene Aufträge, Aufnahme.
- Der Worker sieht nur die Aufnahme und liefert den Abdruck zurück. Verglichen wird nur hier; Profile
  verlassen die Zentrale nie.
- Verglichen wird nur mit Anwesenden der Session (die der Aufnahme zugestimmt haben). Aus Discord-Spuren wird nie
  gelernt.

Abdruck: `effektiv` = normiert(W_BASIS · Basis + Summe gelernter Abdrücke). Die eigene Aufnahme zählt so viel wie
drei gelernte Sessions; danach verbessert sich das Profil mit der echten Tischakustik.

Modellfassung: Abdrücke sind nur mit derselben Fassung des Sprechermodells vergleichbar (siehe app/modelle.py).
Liefert ein Worker Stimmen aus einer anderen Fassung, wird das Profil dafür nicht verwendet. Nach der
bestätigten Zuordnung wird es aus dieser Session neu angelernt (nur mit „aus Sessions lernen“); sonst wird es
verworfen und die Person gebeten, die Stimmprobe neu aufzunehmen.
"""
from __future__ import annotations

import json
import math
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import modelle, storage
from app.db import utcnow
from app.models import GameSession, Job, Member, Speaker, VoiceProfile

MAX_BYTES = 20 * 1024 * 1024
MIN_SPRECHZEIT = 12.0  # Sekunden erkannte Sprache in der Aufnahme
W_BASIS = 3.0
# Kosinus-Ähnlichkeit → Sicherheit (0 unterhalb SIM_UNTEN, 0,95 ab SIM_OBEN). Mit echten Aufnahmen nachjustieren.
SIM_UNTEN, SIM_OBEN = 0.35, 0.75
LERNEN_MIN_SEKUNDEN = 60.0  # nur aus Stimmen mit genug Redezeit lernen
LERNEN_MIN_SIM = 0.45  # Schutz gegen falsch bestätigte oder vermischte Stimmgruppen
AUDIO_TYPEN = ("audio/", "video/webm", "video/mp4", "application/ogg", "application/octet-stream")


# ---------------------------------------------------------------- Vektoren (ohne numpy, die Zentrale bleibt schlank)
def _norm(v: list[float]) -> list[float]:
    laenge = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / laenge for x in v]


def kosinus(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(_norm(a), _norm(b)))


def effektiv(vp: VoiceProfile | None) -> list[float] | None:
    if vp is None or vp.status != "ready" or not vp.base_embedding:
        return None
    basis = _norm(json.loads(vp.base_embedding))
    gelernt = json.loads(vp.learned_sum) if vp.learned_sum else None
    if gelernt and len(gelernt) == len(basis):
        return _norm([W_BASIS * b + g for b, g in zip(basis, gelernt)])
    return basis


def sicherheit(sim: float) -> float:
    if sim <= SIM_UNTEN:
        return 0.0
    return round(min(0.95, 0.95 * (sim - SIM_UNTEN) / (SIM_OBEN - SIM_UNTEN)), 3)


# ---------------------------------------------------------------- Anlegen und Ergebnis
def audio_pfad(job_id: str):
    return storage.uploads_root().parent / "voice" / f"{job_id}.audio"


def anlegen(db: Session, user_id: str, daten: bytes, lernen: bool | None) -> VoiceProfile:
    """Aufnahme speichern, Auftrag für einen Worker anlegen. Ein vorhandenes Profil wird ersetzt:
    alter Abdruck und alles Gelernte werden sofort gelöscht."""
    vp = db.get(VoiceProfile, user_id)
    if vp is None:
        vp = VoiceProfile(user_id=user_id, learn_from_sessions=False, learned_session_count=0)
        db.add(vp)
    _abbrechen(db, user_id)
    vp.status, vp.message = "processing", None
    vp.base_embedding = vp.learned_sum = vp.embedding_model = None
    vp.learned_session_count, vp.sample_seconds = 0, None
    vp.consent_at = utcnow()
    if lernen is not None:
        vp.learn_from_sessions = lernen
    job = Job(type="voice_enroll", owner_user_id=user_id, required_capability="asr", engine="local")
    db.add(job)
    db.flush()
    storage.write_atomic(audio_pfad(job.id), daten)
    return vp


def ergebnis(db: Session, job: Job, embedding: list[float], sprechzeit: float, modell: str | None) -> None:
    vp = db.get(VoiceProfile, job.owner_user_id)
    audio_pfad(job.id).unlink(missing_ok=True)  # Audio sofort weg
    if vp is None or vp.status != "processing":
        return  # inzwischen gelöscht oder ersetzt – Abdruck verwerfen
    if sprechzeit < MIN_SPRECHZEIT:
        vp.status = "failed"
        vp.message = (f"In der Aufnahme wurden nur {sprechzeit:.0f} Sekunden Sprache erkannt. Bitte etwa 20–30 Sekunden "
                      "in ruhiger Umgebung vorlesen.")
        return
    vp.base_embedding = json.dumps([round(float(x), 6) for x in embedding])
    vp.embedding_model, vp.sample_seconds = modell, round(sprechzeit, 1)
    vp.status, vp.message, vp.created_at = "ready", None, utcnow()


def fehlgeschlagen(db: Session, job: Job, message: str) -> None:
    """Endgültiger Fehlschlag eines voice_enroll-Auftrags (von queue.fail_job aufgerufen)."""
    audio_pfad(job.id).unlink(missing_ok=True)
    vp = db.get(VoiceProfile, job.owner_user_id)
    if vp is not None and vp.status == "processing":
        vp.status, vp.message = "failed", f"Die Aufnahme konnte nicht verarbeitet werden: {message}"


def _abbrechen(db: Session, user_id: str) -> None:
    for job in db.scalars(select(Job).where(Job.type == "voice_enroll", Job.owner_user_id == user_id,
                                            Job.state.in_(("queued", "leased")))):
        job.state, job.error_code, job.finished_at = "failed", "cancelled", utcnow()
        job.lease_expires_at = None
        audio_pfad(job.id).unlink(missing_ok=True)


def loeschen(db: Session, user_id: str) -> None:
    """Stimmprofil und alles daraus Gelernte endgültig löschen."""
    _abbrechen(db, user_id)
    vp = db.get(VoiceProfile, user_id)
    if vp is not None:
        db.delete(vp)


def verwaiste_aufnahmen_loeschen(db: Session, alter: timedelta = timedelta(days=1)) -> int:
    """Wartung: Aufnahmen ohne laufenden Auftrag oder älter als einen Tag entfernen."""
    ordner = audio_pfad("x").parent
    if not ordner.exists():
        return 0
    grenze = utcnow() - alter
    n = 0
    for datei in ordner.glob("*.audio"):
        job = db.get(Job, datei.stem)
        if job is None or job.state in ("done", "failed") or job.created_at < grenze:
            datei.unlink(missing_ok=True)
            n += 1
    return n


# ---------------------------------------------------------------- Vergleich und Lernen
def _profile_der_anwesenden(db: Session, s: GameSession, modell: str | None) -> dict[str, list[float]]:
    """member_id → effektiver Abdruck, nur für Anwesende mit fertigem Profil aus derselben Modellfassung."""
    ids = [a.member_id for a in s.attendees if a.member_id]
    out = {}
    for m in db.scalars(select(Member).where(Member.id.in_(ids), Member.user_id.is_not(None))) if ids else []:
        vp = db.get(VoiceProfile, m.user_id)
        v = effektiv(vp)
        if v is not None and modelle.passt(modell, vp.embedding_model):
            out[m.id] = v
    return out


def hinweise(db: Session, s: GameSession) -> list:
    """Hinweise „Stimme ≈ Mitglied“ für die Verteilung in zuordnung.py (Quelle voice_match)."""
    from app.zuordnung import Hinweis

    stimmen = [sp for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id)) if sp.embedding]
    if not stimmen:
        return []
    profile = _profile_der_anwesenden(db, s, stimmen[0].embedding_model)
    if not profile:
        return []
    out = []
    for sp in stimmen:
        v = json.loads(sp.embedding)
        for member_id, p in profile.items():
            c = sicherheit(kosinus(v, p))
            if c > 0:
                out.append(Hinweis(sp.id, member_id, c, "voice_match"))
    return out


def lernen(db: Session, s: GameSession) -> int:
    """Nach bestätigter Zuordnung (vor dem Löschen der Abdrücke): Profile mit „aus Sessions lernen“ verbessern.
    Je Person und Session höchstens einmal, aus der Stimme mit der meisten Redezeit. Nie bei Discord."""
    if s.source == "discord":
        return 0
    member_user = {m.id: m.user_id for m in db.scalars(
        select(Member).where(Member.id.in_([a.member_id for a in s.attendees if a.member_id]),
                             Member.user_id.is_not(None)))}
    beste: dict[str, Speaker] = {}
    for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id)):
        if sp.embedding and sp.assigned_member_id in member_user and sp.speaking_seconds >= LERNEN_MIN_SEKUNDEN:
            alt = beste.get(sp.assigned_member_id)
            if alt is None or sp.speaking_seconds > alt.speaking_seconds:
                beste[sp.assigned_member_id] = sp
    neu_angelernt = _neue_modellfassung(db, s, member_user, beste)
    gelernt = len(neu_angelernt)
    for member_id, sp in beste.items():
        if member_id in neu_angelernt:
            continue
        vp = db.get(VoiceProfile, member_user[member_id])
        profil = effektiv(vp)
        if profil is None or not vp.learn_from_sessions or not modelle.passt(sp.embedding_model, vp.embedding_model):
            continue
        v = json.loads(sp.embedding)
        if len(v) != len(profil) or kosinus(v, profil) < LERNEN_MIN_SIM:
            continue  # passt nicht zum Profil – lieber nichts lernen
        summe = json.loads(vp.learned_sum) if vp.learned_sum else [0.0] * len(v)
        vp.learned_sum = json.dumps([round(a + b, 6) for a, b in zip(summe, _norm(v))])
        vp.learned_session_count += 1
        gelernt += 1
    return gelernt


VERALTET = ("Das Sprechermodell wurde aktualisiert, dein bisheriger Stimmabdruck passt nicht mehr dazu. "
            "Bitte die Stimmprobe neu aufnehmen.")


def _neue_modellfassung(db: Session, s: GameSession, member_user: dict[str, str], beste: dict[str, Speaker]) -> set[str]:
    """Stimmen dieser Session stammen aus einer anderen Fassung des Sprechermodells als manche Profile:
    mit „aus Sessions lernen“ aus der bestätigten Stimme neu anlernen, sonst das Profil verwerfen."""
    modell = next((sp.embedding_model for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id))
                   if sp.embedding and sp.embedding_model), None)
    if modell is None:
        return set()
    neu = set()
    for member_id, user_id in member_user.items():
        vp = db.get(VoiceProfile, user_id)
        if vp is None or vp.status != "ready" or modelle.passt(modell, vp.embedding_model):
            continue
        if vp.learn_from_sessions:
            sp = beste.get(member_id)
            if sp is None or sp.embedding_model != modell:
                continue  # zu wenig Redezeit in dieser Session – nächstes Mal
            vp.base_embedding = json.dumps([round(x, 6) for x in _norm(json.loads(sp.embedding))])
            vp.learned_sum, vp.learned_session_count, vp.embedding_model = None, 1, modell
            neu.add(member_id)
        else:
            vp.base_embedding = vp.learned_sum = vp.embedding_model = None
            vp.learned_session_count, vp.status, vp.message = 0, "failed", VERALTET
    return neu
