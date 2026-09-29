"""Stimmen automatisch zuordnen – läuft auf dem Server, direkt nach der Transkription.

Aufbau: Jede Quelle liefert Hinweise „Stimme S ist vermutlich Mitglied M, Sicherheit c“. Danach verteilt
`verteilen` die Mitglieder so auf die Stimmen, dass jedes Mitglied höchstens einmal vorgeschlagen wird
(die SL kann später trotzdem zwei Stimmen derselben Person zuordnen).

Quellen:
- `intro_round`: Vorstellungsrunde im Transkript („Ich bin Lilio und spiele Jemma Reed“, „ich leite heute“).
- `voice_match`: Abgleich mit Stimmprofilen (app/stimmprofile.py).

Grundsätze: Nur Anwesende mit Konto kommen in Frage. Nur Vorschläge ab MIN_SICHERHEIT. Die SL bestätigt immer.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GameSession, Member, Speaker, TranscriptSegment

FENSTER_SEKUNDEN = 20 * 60  # Vorstellungen stehen am Anfang
MIN_AEHNLICHKEIT = 0.78  # „Gemma“ ≈ „Jemma“ (Whisper-Schreibweise) = 0.8
MIN_SICHERHEIT = 0.5


@dataclass
class Hinweis:
    speaker_id: str
    member_id: str
    sicherheit: float
    quelle: str  # intro_round | voice_match
    beleg: str = ""


# ---------------------------------------------------------------- Namen
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w\s-]", " ", text).strip()


@dataclass
class Person:
    member: Member
    namen: set[str]  # Name, Vorname, Benutzername
    figuren: set[str]  # Charaktername und dessen Teile


def _varianten(text: str | None) -> set[str]:
    if not text:
        return set()
    voll = _norm(text)
    teile = {t for t in re.split(r"[\s-]+", voll) if len(t) >= 3}
    return {voll, *teile} - {""}


def personen(mitglieder: list[Member]) -> list[Person]:
    # Gelöschte Konten (ohne user) haben keinen Namen mehr, an dem man sie erkennen könnte
    return [Person(m, (_varianten(m.user.display_name) | _varianten(m.user.username)) if m.user else set(),
                   _varianten(m.character_name)) for m in mitglieder]


def _aehnlichkeit(wort: str, varianten: set[str]) -> float:
    best = 0.0
    for v in varianten:
        if wort == v:
            return 1.0
        if abs(len(wort) - len(v)) <= 3:
            best = max(best, SequenceMatcher(None, wort, v).ratio())
    return best if best >= MIN_AEHNLICHKEIT else 0.0


def _bester(phrase: str, pers: list[Person], feld: str) -> tuple[Person | None, float]:
    """Wer passt zu den ersten Wörtern nach „ich bin …“ / „ich spiele …“? Auch zwei Wörter („Jemma Reed“)."""
    woerter = [w for w in _norm(phrase).split() if w not in _FUELLWOERTER][:3]
    kandidaten = woerter + [" ".join(woerter[:2])] if len(woerter) >= 2 else woerter
    best, wert = None, 0.0
    for p in pers:
        for w in kandidaten:
            a = _aehnlichkeit(w, getattr(p, feld))
            if a > wert:
                best, wert = p, a
    return best, wert


_FUELLWOERTER = {"der", "die", "das", "den", "dem", "ein", "eine", "einen", "heute", "hier", "also", "ja", "äh", "ähm",
                 "the", "a", "an", "today", "uh", "um", "so", "well", "mal", "wieder", "nochmal", "weiterhin"}

# Wer ich bin
_PERSON = re.compile(r"\b(?:ich bin|ich heiße|ich heisse|mein name ist|hier ist|i am|i'm|my name is|this is)\s+(?P<rest>.{1,40})",
                     re.IGNORECASE)
# Wen ich spiele
_FIGUR = re.compile(r"\b(?:ich spiele|ich spiel|spiele ich|mein charakter heißt|mein charakter ist|meine figur heißt|"
                    r"meine figur ist|mein held heißt|meine heldin heißt|i play|i'm playing|i am playing|"
                    r"my character is|my character's name is)\s+(?P<rest>.{1,40})", re.IGNORECASE)
# Spielleitung
_SL = re.compile(r"\b(?:ich leite|ich bin (?:heute )?(?:euer |eure |der |die |dein |deine )?(?:spielleit\w*|sl|meister\w*|"
                 r"gm|dm|erzähler\w*)|ich mache (?:heute )?(?:die |den )?(?:spielleitung|sl|meister)|"
                 r"i'm (?:your |the )?(?:gm|dm|game ?master)|i am (?:your |the )?(?:gm|dm|game ?master)|i'll be running)",
                 re.IGNORECASE)


# ---------------------------------------------------------------- Vorstellungsrunde
def _absaetze(segmente: list[TranscriptSegment]) -> list[tuple[str, float, str]]:
    """Aufeinanderfolgende Zeilen derselben Stimme verbinden – eine Vorstellung verteilt sich oft auf zwei Zeilen."""
    out: list[list] = []
    for seg in segmente:
        if not seg.speaker_id:
            continue
        if out and out[-1][0] == seg.speaker_id and seg.start - out[-1][3] < 2.5:
            out[-1][2] += " " + seg.text
            out[-1][3] = seg.end
        else:
            out.append([seg.speaker_id, seg.start, seg.text, seg.end])
    return [(a[0], a[1], a[2]) for a in out]


def vorstellungsrunde(segmente: list[TranscriptSegment], mitglieder: list[Member]) -> list[Hinweis]:
    pers = personen(mitglieder)
    sl = [p for p in pers if p.member.role == "gm"]
    hinweise: list[Hinweis] = []
    for speaker_id, start, text in _absaetze(segmente):
        if start > FENSTER_SEKUNDEN:
            break
        zitat = text.strip()[:160]
        for m in _PERSON.finditer(text):
            p, a = _bester(m.group("rest"), pers, "namen")
            if p:
                hinweise.append(Hinweis(speaker_id, p.member.id, 0.9 * a, "intro_round", zitat))
            else:  # „ich bin heute Litha Flamel“ – nach „ich bin“ steht manchmal die Figur
                p, a = _bester(m.group("rest"), pers, "figuren")
                if p:
                    hinweise.append(Hinweis(speaker_id, p.member.id, 0.8 * a, "intro_round", zitat))
        for m in _FIGUR.finditer(text):
            p, a = _bester(m.group("rest"), pers, "figuren")
            if p:
                hinweise.append(Hinweis(speaker_id, p.member.id, 0.85 * a, "intro_round", zitat))
        if len(sl) == 1 and _SL.search(text):
            hinweise.append(Hinweis(speaker_id, sl[0].member.id, 0.8, "intro_round", zitat))
    return hinweise


# ---------------------------------------------------------------- Verteilen
def verteilen(hinweise: list[Hinweis]) -> dict[str, Hinweis]:
    """Hinweise je (Stimme, Mitglied) zusammenfassen (1 − Π(1 − c)), dann gierig nach Sicherheit verteilen:
    jede Stimme und jedes Mitglied höchstens einmal. Ergebnis: speaker_id → bester Hinweis."""
    summe: dict[tuple[str, str], Hinweis] = {}
    for h in hinweise:
        k = (h.speaker_id, h.member_id)
        if k in summe:
            alt = summe[k]
            alt.sicherheit = 1 - (1 - alt.sicherheit) * (1 - h.sicherheit)
            if h.quelle == "voice_match":  # Stimmprofil ist die stärkere Quelle
                alt.quelle = "voice_match"
        else:
            summe[k] = Hinweis(h.speaker_id, h.member_id, h.sicherheit, h.quelle, h.beleg)
    # Widersprüche abwerten: sagt dieselbe Stimme für zwei Personen „ich bin …“, ist sie unsicherer
    je_stimme: dict[str, float] = {}
    for h in summe.values():
        je_stimme[h.speaker_id] = je_stimme.get(h.speaker_id, 0) + h.sicherheit
    for h in summe.values():
        h.sicherheit *= h.sicherheit / je_stimme[h.speaker_id]  # nur ein Kandidat: unverändert
    ergebnis: dict[str, Hinweis] = {}
    vergeben: set[str] = set()
    for h in sorted(summe.values(), key=lambda x: -x.sicherheit):
        if h.sicherheit < MIN_SICHERHEIT or h.speaker_id in ergebnis or h.member_id in vergeben:
            continue
        ergebnis[h.speaker_id] = h
        vergeben.add(h.member_id)
    return ergebnis


def vorschlagen(db: Session, s: GameSession, weitere_hinweise: list[Hinweis] | None = None) -> int:
    """Vorschläge für die Stimmen einer Tischaufnahme setzen. Gibt die Anzahl vorgeschlagener Stimmen zurück."""
    anwesend = {a.member_id for a in s.attendees if a.member_id}
    mitglieder = list(db.scalars(select(Member).where(Member.id.in_(anwesend)))) if anwesend else []
    segmente = list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)
                               .order_by(TranscriptSegment.position)))
    from app.stimmprofile import hinweise as stimm_hinweise

    hinweise = vorstellungsrunde(segmente, mitglieder) + stimm_hinweise(db, s) + list(weitere_hinweise or [])
    auswahl = verteilen(hinweise)
    for sp in db.scalars(select(Speaker).where(Speaker.session_id == s.id)):
        if sp.source == "discord_track":
            continue
        h = auswahl.get(sp.id)
        if h is None:
            sp.suggested_member_id, sp.confidence, sp.source = None, 0.0, "none"
        else:
            sp.suggested_member_id, sp.confidence, sp.source = h.member_id, round(h.sicherheit, 2), h.quelle
    return len(auswahl)
