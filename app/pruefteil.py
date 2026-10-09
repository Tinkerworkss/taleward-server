"""Prüfteil des Recaps (Schnittstelle 0.4.6, Recap.review) – nur für die SL.

Die Gegenprüfung (Stufe 3) läuft im Sprachmodell (`sprachmodell.Ablauf.gegenpruefen`) und liefert je Absatz ein
Urteil mit Stellen (Zeitangabe und Zitat). Hier setzt die Zentrale diese Stellen mechanisch auf das echte Transkript
(Stufe 1): Zitat gefunden → Wortlaut und Zeitpunkt des Transkripts; nur eine Zeitangabe → der Abschnitt an dieser
Stelle. So sieht die SL immer, was wirklich gesagt wurde, und wer es gesagt hat.

Der Server streicht nichts; die Urteile sind Hinweise für die SL.

0.4.64: Die App markiert nur noch beanstandete Absätze und zeigt dort die Begründung als einen Satz. Darum hier:
Begründungen höchstens ein kurzer Satz (`kurz`), bei „belegt“ keine; ein „unbelegt“, zu dem das Modell selbst eine
Stelle nennt, die sich im Transkript wiederfindet, ist höchstens „teilweise“ – sonst bleibt die Ansicht zu voll.
"""
from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass

from app import belege
from app.db import utcnow

URTEILE = ("supported", "partial", "unsupported", "contradicted", "off_game", "unchecked")
BERICHT = {"supported": "supported", "partial": "partial", "unsupported": "unsupported",
           "contradicted": "contradicted", "off_game": "offGame"}
OHNE_STELLE = {"de": "Keine Belegstelle im Transkript gefunden.", "en": "No supporting passage found in the transcript."}
MAX_BELEGE = 3
NOTIZ_HOECHSTENS = 220  # Zeichen
_SATZGRENZE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"])")
_VORSPANN = re.compile(r"^\s*(?:absatz|paragraph)\s*\d+\s*[:.–-]\s*", re.I)


def kurz(notiz: str | None) -> str | None:
    """Begründung als ein Satz für Menschen: Vorspann „Absatz 3:“ weg, nur der erste Satz (außer in Anführungszeichen),
    höchstens NOTIZ_HOECHSTENS Zeichen."""
    t = _VORSPANN.sub("", " ".join(str(notiz or "").split()))
    if not t:
        return None
    stueck, offen = [], 0
    for teil in _SATZGRENZE.split(t):  # erster Satz; ein Satzende innerhalb von „…“ zählt nicht
        stueck.append(teil)
        offen += teil.count("„") - teil.count("“")
        if offen <= 0:
            break
    t = " ".join(stueck)
    if len(t) > NOTIZ_HOECHSTENS:
        t = t[:NOTIZ_HOECHSTENS - 1].rsplit(" ", 1)[0].rstrip(" ,;:–-") + " …"
    return t


@dataclass
class Abschnitt:
    start: float
    end: float
    text: str
    member_id: str | None


class Stellen:
    """Transkript-Abschnitte mit Suche nach Zeit und Zitat."""

    def __init__(self, abschnitte: list[Abschnitt]):
        self.abschnitte = sorted(abschnitte, key=lambda a: a.start)
        self.anfaenge = [a.start for a in self.abschnitte]
        self.woerter = belege.Transkript([(a.start, a.text) for a in self.abschnitte])

    def bei(self, zeit: float) -> Abschnitt | None:
        """Abschnitt, der zu dieser Zeit läuft – sonst der nächstgelegene."""
        if not self.abschnitte:
            return None
        i = bisect_right(self.anfaenge, zeit) - 1
        kandidaten = [self.abschnitte[j] for j in (i, i + 1) if 0 <= j < len(self.abschnitte)]
        return min(kandidaten, key=lambda a: 0 if a.start <= zeit <= a.end else min(abs(a.start - zeit),
                                                                                         abs(a.end - zeit)))

    def beleg(self, start: float | None, zitat: str) -> dict | None:
        treffer = self.woerter.suchen(zitat, start) if zitat.strip() else None
        if treffer is not None:
            zeit, wortlaut = treffer
        elif start is not None:
            a = self.bei(start)
            if a is None or min(abs(a.start - start), abs(a.end - start)) > 90 and not a.start <= start <= a.end:
                return None
            zeit, wortlaut = a.start, a.text.strip()[:200]
        else:
            return None
        a = self.bei(zeit)
        return {"start": round(zeit, 2), "end": round(a.end, 2) if a else None, "quote": wortlaut,
                "speakerMemberId": a.member_id if a else None}


def leer(state: str = "skipped") -> dict:
    return {"state": state, "stale": False, "checkedAt": None, "model": None, "revised": False,
            "report": {"total": 0, "supported": 0, "partial": 0, "unsupported": 0, "contradicted": 0, "offGame": 0},
            "paragraphs": []}


def bauen(roh: dict | None, text: str, stellen: Stellen, sprache: str = "de") -> dict:
    """Prüfteil aus der Antwort der Gegenprüfung. roh None → skipped (abgeschaltet oder ältere Worker-Fassung)."""
    if not roh:
        return leer()
    from app.sprachmodell import absaetze

    anzahl = len(absaetze(text))
    nach_index = {}
    for p in roh.get("paragraphs") or []:
        if isinstance(p, dict) and isinstance(p.get("index"), int) and 0 <= p["index"] < anzahl:
            nach_index.setdefault(p["index"], p)
    absaetze_aus = []
    for i in range(anzahl):
        p = nach_index.get(i) or {}
        urteil = p.get("verdict") if p.get("verdict") in URTEILE else "unchecked"
        gefunden, gesehen = [], set()
        for b in p.get("evidence") or []:
            if not isinstance(b, dict):
                continue
            start = b.get("start") if isinstance(b.get("start"), (int, float)) else None
            beleg = stellen.beleg(start, str(b.get("quote") or ""))
            if beleg and beleg["start"] not in gesehen:
                gesehen.add(beleg["start"])
                gefunden.append(beleg)
        notiz = kurz(p.get("note"))
        if urteil == "supported" and not gefunden:
            urteil = "partial"
            notiz = notiz or OHNE_STELLE.get(sprache, OHNE_STELLE["de"])
        elif urteil == "supported":
            notiz = None
        elif urteil == "unsupported" and gefunden:
            urteil = "partial"  # das Modell nennt selbst eine Stelle, die es im Transkript gibt
        absaetze_aus.append({"index": i, "verdict": urteil, "note": notiz,
                             "evidence": gefunden[:MAX_BELEGE]})
    bericht = {"total": anzahl, "supported": 0, "partial": 0, "unsupported": 0, "contradicted": 0, "offGame": 0}
    for a in absaetze_aus:
        if a["verdict"] in BERICHT:
            bericht[BERICHT[a["verdict"]]] += 1
    return {"state": "done", "stale": False, "checkedAt": utcnow().isoformat().replace("+00:00", "Z"),
            "model": (str(roh.get("model") or "")[:100] or None), "revised": bool(roh.get("revised")),
            "report": bericht, "paragraphs": absaetze_aus}


def lesen(roh: str | None) -> dict:
    if not roh:
        return leer()
    try:
        d = json.loads(roh)
    except ValueError:
        return leer()
    return d if isinstance(d, dict) and d.get("state") in ("pending", "done", "skipped") else leer()


def als_json(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)
