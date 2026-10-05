"""Konservative Terminologie-Nachkorrektur nach der ASR.

Wichtig: Dieser Pass verändert keine Segmentgrenzen und ergänzt keinen Inhalt.
Er ersetzt nur ganze Wörter/Phrasen, wenn eine explizite, kampagnen- oder
systemseitig bestätigte Zuordnung existiert.

Quellen:
- kuratierte systembezogene Verhörer (z. B. "Nujen" -> "Nuyen"),
- von der SL bestätigte Korrekturen aus früheren Sessions,
- reine Orthografievarianten bekannter Kampagnen-/Systembegriffe
  (z. B. Bindestrich/Punkt), sofern die Variante eindeutig ist.

Keine fuzzy/semantische Autokorrektur: "Yen" -> "Nuyen" oder
"Seda Group" -> "Saeder-Krupp" werden ohne explizite Zuordnung NICHT blind
ersetzt. Solche Fälle bleiben für die Unsicherheitsprüfung sichtbar.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from app import namenshilfe, systembegriffe as begriffslisten

VERSION = "1"


@dataclass(frozen=True)
class Regel:
    gehoert: str
    korrekt: str
    quelle: str


def _muster(text: str) -> re.Pattern:
    teile = [re.escape(t) for t in text.split()]
    innen = r"\s+".join(teile)
    return re.compile(rf"(?<!\w){innen}(?!\w)", re.IGNORECASE)


def _orthografie_varianten(kanonisch: str) -> set[str]:
    """Nur Varianten, die sich ausschließlich durch harmlose Satzzeichen unterscheiden."""
    aus: set[str] = set()
    if "-" in kanonisch:
        aus.add(kanonisch.replace("-", " "))
    if "." in kanonisch:
        aus.add(kanonisch.replace(".", ""))
        aus.add(kanonisch.replace(".", " "))
    return {" ".join(x.split()) for x in aus if x.strip() and x.casefold() != kanonisch.casefold()}


def regeln(campaign) -> list[Regel]:
    liste = begriffslisten.erkennen(campaign.system, campaign.system_name)
    mapping: dict[str, Regel] = {}

    # Systemregeln zuerst.
    if liste is not None:
        for gehoert, korrekt in liste.verhoerer_fuer(campaign.language):
            mapping[gehoert.casefold()] = Regel(gehoert, korrekt, "system")

    # Orthografie nur, wenn eine Variante genau einem kanonischen Begriff entspricht.
    varianten: dict[str, set[str]] = {}
    if liste is not None:
        for kanonisch in liste.alle_fuer(campaign.language):
            for v in _orthografie_varianten(kanonisch):
                varianten.setdefault(v.casefold(), set()).add(kanonisch)
    for kanonisch in namenshilfe.korrekturen(campaign).values():
        for v in _orthografie_varianten(kanonisch):
            varianten.setdefault(v.casefold(), set()).add(kanonisch)
    for v, ziele in varianten.items():
        if len(ziele) == 1 and v not in mapping:
            ziel = next(iter(ziele))
            mapping[v] = Regel(v, ziel, "orthography")

    # Bestätigte Kampagnenkorrekturen haben Vorrang vor Systemregeln.
    for gehoert, korrekt in namenshilfe.korrekturen(campaign).items():
        mapping[gehoert.casefold()] = Regel(gehoert, korrekt, "learned")

    return sorted(mapping.values(), key=lambda r: len(r.gehoert), reverse=True)


def korrigieren(text: str | None, campaign, vorbereitete_regeln: list[Regel] | None = None) -> tuple[str | None, Counter]:
    """Text korrigieren und Zähler nach Quelle zurückgeben."""
    if not text:
        return text, Counter()
    zaehler: Counter[str] = Counter()
    aus = text
    for regel in vorbereitete_regeln if vorbereitete_regeln is not None else regeln(campaign):
        muster = _muster(regel.gehoert)
        aus, n = muster.subn(regel.korrekt, aus)
        if n:
            zaehler[regel.quelle] += n
    return aus, zaehler
