"""Qualitätsprüfung, Stufe 1: Belege der Vorschläge mechanisch gegen das Transkript prüfen (ohne KI).

Das Sprachmodell nennt zu jedem Vorschlag bis zu drei Belege (Zeitpunkt + wörtliches Zitat). Modelle erfinden aber
gelegentlich Zitate oder verrutschen beim Zeitpunkt. Hier wird jedes Zitat im Transkript gesucht – mit Toleranz für
Erkennungsfehler und abweichende Satzzeichen:

- gefunden → Zeitpunkt auf den Beginn der Fundstelle gesetzt, Zitat durch den Wortlaut des Transkripts ersetzt
  (die SL sieht also immer, was wirklich gesagt wurde)
- nicht gefunden → Beleg entfällt

Bleibt bei einem Vorschlag kein Beleg übrig, bekommt er `low_confidence` und höchstens 0,3 Sicherheit. Gestrichen
wird nichts – die SL entscheidet (plan-qualitaetspruefung.md).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

SCHWELLE = 0.78            # Ähnlichkeit ab der ein Zitat als gefunden gilt
FENSTER_S = 120.0          # zuerst so nah am genannten Zeitpunkt suchen, dann im ganzen Transkript
MAX_ANKER = 300            # Fundstellen je Ankerwort, die genauer verglichen werden
KEIN_BELEG_SICHERHEIT = 0.3

_TOKEN = re.compile(r"\S+")


def normal(wort: str) -> str:
    """Kleinschreibung, ohne Satzzeichen; Umlaute bleiben (ä ≠ a), Sonderformen (ﬁ, ſ) werden vereinheitlicht."""
    w = unicodedata.normalize("NFKC", wort).casefold()
    return "".join(z for z in w if z.isalnum())


@dataclass
class _Wort:
    norm: str
    roh: str
    start: float


class Transkript:
    """Wortliste über alle Segmente, mit Index für schnelle Suche."""

    def __init__(self, segmente: list[tuple[float, str]]):
        self.woerter: list[_Wort] = []
        for start, text in segmente:
            for t in _TOKEN.findall(text or ""):
                n = normal(t)
                if n:
                    self.woerter.append(_Wort(n, t, float(start)))
        self.index: dict[str, list[int]] = {}
        for i, w in enumerate(self.woerter):
            self.index.setdefault(w.norm, []).append(i)

    def suchen(self, zitat: str, zeit: float | None = None) -> tuple[float, str] | None:
        """(Beginn der Fundstelle in Sekunden, Wortlaut im Transkript) oder None."""
        q = [n for n in (normal(t) for t in _TOKEN.findall(zitat or "")) if n]
        if not q or not self.woerter:
            return None
        if zeit is not None:
            treffer = self._suchen(q, lambda i: abs(self.woerter[i].start - zeit) <= FENSTER_S)
            if treffer:
                return treffer
        return self._suchen(q, lambda _i: True)

    def _suchen(self, q: list[str], erlaubt) -> tuple[float, str] | None:
        n = len(q)
        q_text = " ".join(q)
        # Anker: die seltensten Wörter des Zitats, die im Transkript überhaupt vorkommen
        vorhanden = sorted({(len(self.index[w]), k, w) for k, w in enumerate(q) if w in self.index})
        if not vorhanden:
            return None
        bester, best_wert = None, 0.0
        geprueft: set[tuple[int, int]] = set()
        genau = n < 3  # sehr kurze Zitate nur wortgleich
        for _anzahl, k, wort in vorhanden[:2]:
            for pos in self.index[wort][:MAX_ANKER]:
                if not erlaubt(pos):
                    continue
                anfang = pos - k
                for a in range(anfang - 2, anfang + 3):
                    for laenge in ([n] if genau else range(max(1, n - 2), n + 3)):
                        if a < 0 or a + laenge > len(self.woerter) or (a, laenge) in geprueft:
                            continue
                        geprueft.add((a, laenge))
                        teil = self.woerter[a:a + laenge]
                        if genau:
                            wert = 1.0 if [w.norm for w in teil] == q else 0.0
                        else:
                            vergleich = SequenceMatcher(None, q_text, " ".join(w.norm for w in teil),
                                                        autojunk=False)
                            # schnelle Obergrenzen zuerst – die genaue Rechnung nur, wenn es reichen kann
                            wert = (vergleich.ratio() if vergleich.real_quick_ratio() >= SCHWELLE
                                    and vergleich.quick_ratio() >= SCHWELLE else 0.0)
                        if wert > best_wert:
                            bester, best_wert = (a, laenge), wert
        if bester is None or best_wert < SCHWELLE:
            return None
        a, laenge = bester
        teil = self.woerter[a:a + laenge]
        return teil[0].start, " ".join(w.roh for w in teil)[:200]


def pruefen(transkript: Transkript, evidence: list[dict]) -> list[dict]:
    """Nur die Belege, die sich im Transkript finden – mit korrigiertem Zeitpunkt und echtem Wortlaut."""
    gut, gesehen = [], set()
    for b in evidence or []:
        if not isinstance(b, dict):
            continue
        zeit = b.get("start")
        treffer = transkript.suchen(str(b.get("quote") or ""),
                                    float(zeit) if isinstance(zeit, (int, float)) and zeit > 0 else None)
        if treffer and treffer not in gesehen:
            gesehen.add(treffer)
            gut.append({"start": round(treffer[0], 2), "quote": treffer[1]})
    return gut
