"""Unsicher erkannte Namen und ihre Korrektur (Schnittstelle 0.4.6, nur für die SL).

Quelle: Der Worker meldet je Abschnitt die Wörter, die die zeitliche Ausrichtung schlecht getroffen hat
(`TranscriptSegment.unsicher`). Daraus werden Kandidaten für die SL:
- nur großgeschriebene Wörter ab drei Buchstaben, nicht am Satzanfang (dort sagt Großschreibung nichts),
- nicht, was bekannt ist (app/woerterbuch.py: allgemeine Wortliste, Texte der Kampagne, in anderen Kapiteln sicher
  Gesagtes) oder was die SL weggeklickt hat,
- gebündelt nach Klang (Kölner Phonetik): „Tharvok“, „Tarvok“ und „Darvok“ sind ein Begriff,
- ähnlich klingende Bibel- und Charakternamen und Namen aus den Texten der Kampagne kommen als Vorschlag mit.

Korrektur: reine Textersetzung (ganze Wörter) in Transkript, Recap, Vorschlägen und Prüfteil – oder, solange das
Audio da ist, eine erneute Transkription mit der korrigierten Namenshilfe (höchstens zweimal je Session).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import namenshilfe, pruefteil
from app.models import Campaign, Entry, GameSession, Member, Proposal, Recap, TranscriptSegment, Upload

MAX_BEGRIFFE = 20
MAX_NACHTRANSKRIPTIONEN = 2
AEHNLICH = 0.72


# ---------------------------------------------------------------- Kölner Phonetik
_KP = {**dict.fromkeys("aeijouyäöü", "0"), "h": "", "b": "1", "f": "3", "v": "3", "w": "3", "g": "4", "k": "4",
       "q": "4", "l": "5", "m": "6", "n": "6", "r": "7", "s": "8", "z": "8", "ß": "8"}


def phonetik(wort: str) -> str:
    """Kölner Phonetik (vereinfacht, für Eigennamen gut genug): gleich klingende Schreibweisen → gleicher Code."""
    w = [z for z in wort.lower() if z.isalpha()]
    code = []
    for i, z in enumerate(w):
        vor = w[i - 1] if i else ""
        nach = w[i + 1] if i + 1 < len(w) else ""
        if z in _KP:
            c = _KP[z]
        elif z == "p":
            c = "3" if nach == "h" else "1"
        elif z in "dt":
            c = "8" if nach in ("c", "s", "z") else "2"
        elif z == "c":
            if i == 0:
                c = "4" if nach in "ahkloqrux" else "8"
            else:
                c = "8" if vor in ("s", "z") or nach not in "ahkoqux" else "4"
        elif z == "x":
            c = "8" if vor in ("c", "k", "q") else "48"
        else:
            c = ""
        code.append(c)
    roh = "".join(code)
    ohne_doppel = "".join(z for i, z in enumerate(roh) if i == 0 or z != roh[i - 1])
    return ohne_doppel[:1] + ohne_doppel[1:].replace("0", "") if ohne_doppel else ""


def aehnlich(a: str, b: str) -> float:
    a, b = a.casefold(), b.casefold()
    wert = SequenceMatcher(None, a, b).ratio()
    if phonetik(a) and phonetik(a) == phonetik(b):
        wert = max(wert, 0.85)
    return wert


# ---------------------------------------------------------------- Begriffe
def _wort(roh: str) -> str:
    return roh.strip(".,;:!?…\"'„“”‚‘()[]«»-–")


def _namen(db: Session, c: Campaign, b) -> list[tuple[str, str | None, str | None]]:
    """(Name, entryId, memberId) für Vorschläge: Bibel, Charaktere und namenartige Wörter aus den Texten der Kampagne."""
    namen: list[tuple[str, str | None, str | None]] = []
    for e in db.scalars(select(Entry).where(Entry.campaign_id == c.id)):
        namen.append((e.name, e.id, None))
    for m in db.scalars(select(Member).where(Member.campaign_id == c.id)):
        if m.character_name:
            namen.append((m.character_name, None, m.id))
    schon = {n.casefold() for n, _, _ in namen}
    namen += [(w, None, None) for k, w in b.namen.items() if k not in schon]
    return namen


def _bekannt(wort: str, bekannt, namen) -> bool:
    """Bekanntes Wort – außer es ist nur als Zusammensetzung bekannt und klingt fast wie ein Name der Kampagne
    („Rabenfeld“ statt „Rabenfels“): dann lieber nachfragen."""
    if bekannt.direkt(wort):
        return True
    if not bekannt.enthaelt(wort):
        return False
    return not any(n.casefold() != wort.casefold() and aehnlich(wort, n) >= 0.85 for n, _, _ in namen)


def begriffe(db: Session, s: GameSession) -> list[dict]:
    from app import systembegriffe as begriffslisten, woerterbuch

    c = db.get(Campaign, s.campaign_id)
    bekannt = woerterbuch.kampagne(db, c, ausser_session_id=s.id)
    namen = _namen(db, c, bekannt)
    weg = namenshilfe.ignoriert(c)
    verhoerer = begriffslisten.verhoerer_map(c.system, c.system_name, sprache=c.language)
    gruppen: dict[str, dict] = {}
    for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id,
                                                          TranscriptSegment.unsicher.is_not(None))
                          .order_by(TranscriptSegment.start)):
        try:
            woerter = json.loads(seg.unsicher or "[]")
        except ValueError:
            continue
        text_woerter = {_wort(t) for t in seg.text.split()}
        for w in woerter:
            wort = _wort(str(w.get("word") or ""))
            if (len(wort) < 3 or not wort[0].isupper() or wort.isupper() or w.get("anfang")
                    or wort.casefold() in weg or wort not in text_woerter or _bekannt(wort, bekannt, namen)):
                continue  # nicht (mehr) im Text: schon korrigiert
            g = gruppen.setdefault(phonetik(wort) or wort.casefold(), {"schreibweisen": {}, "werte": [], "beispiele": []})
            g["schreibweisen"][wort] = g["schreibweisen"].get(wort, 0) + 1
            g["werte"].append(float(w.get("score") or 0))
            if len(g["beispiele"]) < 3 and all(b["start"] != seg.start for b in g["beispiele"]):
                g["beispiele"].append({"start": round(seg.start, 2), "quote": seg.text.strip()[:200]})
    aus = []
    for g in gruppen.values():
        heard = max(g["schreibweisen"], key=lambda k: (g["schreibweisen"][k], k))
        vorschlaege = sorted(((aehnlich(heard, n), n, eid, mid) for n, eid, mid in namen), reverse=True)
        passend = [v for v in vorschlaege if v[0] >= AEHNLICH]
        andere = [k for k in sorted(g["schreibweisen"], key=lambda k: -g["schreibweisen"][k]) if k != heard]
        # Systembezogene bekannte Verhörer nur als Vorschlag, nie blind automatisch ersetzen.
        # Existiert die Schreibweise bereits als Kampagnenname, wurde sie oben als bekannt ausgesiebt.
        direkt = verhoerer.get(heard.casefold())
        alternativen = list(dict.fromkeys(([direkt] if direkt else []) + [v[1] for v in passend[:2]] + andere))[:4]
        vorkommen = sum(g["schreibweisen"].values())
        sicherheit = round(sum(g["werte"]) / len(g["werte"]), 3)
        bester = passend[0] if passend else None
        aus.append({
            "id": hashlib.sha1(f"{s.id}:{heard.casefold()}".encode()).hexdigest()[:16],
            "heard": heard, "alternatives": alternativen, "occurrences": vorkommen,
            "confidence": max(0.0, min(1.0, sicherheit)), "examples": g["beispiele"],
            "suggestedEntryId": bester[2] if bester else None, "suggestedMemberId": bester[3] if bester else None,
            "_rang": (bool(direkt or passend), vorkommen * (1 - sicherheit)),
        })
    aus.sort(key=lambda t: t["_rang"], reverse=True)
    for t in aus:
        del t["_rang"]
    return aus[:MAX_BEGRIFFE]


# ---------------------------------------------------------------- Audio
def audio_da(db: Session, s: GameSession) -> bool:
    if s.audio_deleted_at is not None:
        return False
    return db.scalar(select(Upload.id).where(Upload.session_id == s.id, Upload.state == "completed")) is not None


def audio_weg_am(db: Session, s: GameSession):
    from app import aufbewahrung

    a = aufbewahrung.lesen(db)
    if not a.bis_freigabe or not audio_da(db, s):
        return None
    fertig = db.scalar(select(Upload.completed_at).where(Upload.session_id == s.id, Upload.state == "completed")
                       .order_by(Upload.completed_at.desc()).limit(1))
    return fertig + timedelta(days=a.tage) if fertig else None


# ---------------------------------------------------------------- Korrektur
def _muster(heard: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(heard)}(?!\w)", re.IGNORECASE)


def _ersetzen(text: str | None, ersetzungen: list[tuple[re.Pattern, str]]) -> str | None:
    if not text:
        return text
    for muster, neu in ersetzungen:
        text = muster.sub(neu, text)
    return text


def ersetzen(db: Session, s: GameSession, paare: list[tuple[str, str]]) -> None:
    """Textersetzung (ganze Wörter, Groß-/Kleinschreibung egal) in allem, was aus dem Transkript entstanden ist."""
    ersetzungen = [(_muster(alt), neu) for alt, neu in paare if alt and neu and alt != neu]
    if not ersetzungen:
        return
    for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.session_id == s.id)):
        seg.text = _ersetzen(seg.text, ersetzungen)
    r = db.get(Recap, s.id)
    if r is not None:
        r.title = _ersetzen(r.title, ersetzungen)[:300]
        r.text = _ersetzen(r.text, ersetzungen)
        r.open_threads = json.dumps([_ersetzen(f, ersetzungen) for f in json.loads(r.open_threads or "[]")],
                                    ensure_ascii=False)
        pruefung = pruefteil.lesen(r.review)
        for p in pruefung.get("paragraphs") or []:
            p["note"] = _ersetzen(p.get("note"), ersetzungen)
            for b in p.get("evidence") or []:
                b["quote"] = _ersetzen(b.get("quote"), ersetzungen)
        r.review = pruefteil.als_json(pruefung)
    for p in db.scalars(select(Proposal).where(Proposal.session_id == s.id)):
        p.title = _ersetzen(p.title, ersetzungen)[:300]
        p.detail = _ersetzen(p.detail, ersetzungen)
        p.gm_notes = _ersetzen(p.gm_notes, ersetzungen)
        try:
            belege = json.loads(p.evidence or "[]")
            for b in belege:
                b["quote"] = _ersetzen(b.get("quote"), ersetzungen)
            p.evidence = json.dumps(belege, ensure_ascii=False)
        except (ValueError, AttributeError):
            pass
