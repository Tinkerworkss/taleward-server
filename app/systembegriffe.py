"""Strukturierte Rollenspiel-Begriffslisten für Whisper und die Namensprüfung.

Dateiformat (rückwärtskompatibel zu den alten einfachen Listen):

    # Erkennungsnamen: dsa, das schwarze auge
    # Stufe A
    Aventurien
    ...
    # Stufe B
    Borbarad
    ...
    # Verhörer
    Aventurin => Aventurien

Stufe A ist klein und darf als Whisper-Hotword verwendet werden.
Stufe B ist das große Nachschlagewörterbuch und wird nur dann zum Hotword
promoviert, wenn der Begriff bereits im Kampagnenkontext vorkommt.
Verhörer sind Korrekturhinweise, keine automatische Textersetzung.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ORDNER = Path(__file__).resolve().parent / "begriffe"
_MAX_SYSTEM_DATEIEN = 100
_MEHRDEUTIGE_ALIASE = {"genesys", "imperium"}  # ohne weiteren Kontext keinem Spezialwörterbuch zuordnen

_HEADER_ALIASE = re.compile(r"^#\s*Erkennungsnamen\s*:\s*(.+)$", re.IGNORECASE)
_HEADER_A = re.compile(r"^#\s*Stufe\s+A\b", re.IGNORECASE)
_HEADER_B = re.compile(r"^#\s*Stufe\s+B\b", re.IGNORECASE)
_HEADER_VERHOERER = re.compile(r"^#\s*Verhörer\b", re.IGNORECASE)


@dataclass(frozen=True)
class Begriffsliste:
    schluessel: str
    aliases: tuple[str, ...]
    stufe_a: tuple[str, ...]
    stufe_b: tuple[str, ...]
    verhoerer: tuple[tuple[str, str], ...]

    @property
    def alle(self) -> tuple[str, ...]:
        return self.stufe_a + self.stufe_b


def _eindeutig(werte) -> tuple[str, ...]:
    aus: list[str] = []
    gesehen: set[str] = set()
    for wert in werte:
        w = " ".join(str(wert or "").split())
        if not w:
            continue
        k = w.casefold()
        if k not in gesehen:
            gesehen.add(k)
            aus.append(w)
    return tuple(aus)


@lru_cache(maxsize=_MAX_SYSTEM_DATEIEN)
def _lesen_pfad(pfad: str) -> Begriffsliste:
    p = Path(pfad)
    abschnitt = "a"  # alte Listen ohne Überschrift bleiben Stufe A
    aliases: list[str] = []
    a: list[str] = []
    b: list[str] = []
    verhoerer: list[tuple[str, str]] = []

    for roh in p.read_text(encoding="utf-8").splitlines():
        zeile = roh.strip()
        if not zeile:
            continue
        m = _HEADER_ALIASE.match(zeile)
        if m:
            aliases.extend(x.strip() for x in m.group(1).split(",") if x.strip())
            continue
        if _HEADER_A.match(zeile):
            abschnitt = "a"
            continue
        if _HEADER_B.match(zeile):
            abschnitt = "b"
            continue
        if _HEADER_VERHOERER.match(zeile):
            abschnitt = "verhoerer"
            continue
        if zeile.startswith("#"):
            continue

        if abschnitt == "verhoerer":
            if "=>" not in zeile:
                continue
            alt, neu = (x.strip() for x in zeile.split("=>", 1))
            if alt and neu and alt.casefold() != neu.casefold():
                verhoerer.append((alt, neu))
        elif abschnitt == "b":
            b.append(zeile)
        else:
            a.append(zeile)

    stufe_a = _eindeutig(a)
    a_keys = {x.casefold() for x in stufe_a}
    stufe_b = tuple(x for x in _eindeutig(b) if x.casefold() not in a_keys)
    return Begriffsliste(
        schluessel=p.stem,
        aliases=_eindeutig(aliases),
        stufe_a=stufe_a,
        stufe_b=stufe_b,
        verhoerer=tuple(verhoerer),
    )


def cache_leeren() -> None:
    _lesen_pfad.cache_clear()
    _alle.cache_clear()


def lesen(schluessel: str | None) -> Begriffsliste | None:
    if not schluessel or schluessel == "other":
        return None
    p = _ORDNER / f"{schluessel}.txt"
    return _lesen_pfad(str(p)) if p.is_file() else None


@lru_cache(maxsize=8)
def _alle(ordner: str) -> tuple[Begriffsliste, ...]:
    p = Path(ordner)
    if not p.is_dir():
        return ()
    return tuple(_lesen_pfad(str(datei)) for datei in sorted(p.glob("*.txt"))[:_MAX_SYSTEM_DATEIEN])


def _normal(text: str | None) -> str:
    # Apostrophe/Bindestriche/&, Punkte etc. als Wortgrenzen behandeln.
    return " ".join(re.findall(r"[^\W_]+", (text or "").casefold(), flags=re.UNICODE))


def _phrase_enthalten(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def erkennen(system: str | None, system_name: str | None = None) -> Begriffsliste | None:
    """Direkten Systemschlüssel nutzen; bei `other` den freien Systemnamen anhand der Aliaszeile erkennen."""
    direkt = lesen(system)
    if direkt is not None:
        return direkt

    name = _normal(system_name)
    if not name:
        return None

    # Solange keine eigene 40k-Liste existiert, Fantasy-Warhammer nicht aus einem 40k-Namen ableiten.
    if any(x in name.split() for x in ("40k", "40000")) or _phrase_enthalten(name, "40 000") or _phrase_enthalten(name, "dark heresy") \
            or _phrase_enthalten(name, "wrath glory") or _phrase_enthalten(name, "rogue trader"):
        return None

    treffer: list[tuple[int, int, Begriffsliste]] = []
    for liste in _alle(str(_ORDNER)):
        for alias in liste.aliases:
            n = _normal(alias)
            if not n or n in _MEHRDEUTIGE_ALIASE:
                continue
            if name == n:
                score = 1000 + len(n)
            elif _phrase_enthalten(name, n):
                score = 100 + len(n)
            else:
                continue
            treffer.append((score, len(n.split()), liste))
    if not treffer:
        return None
    treffer.sort(key=lambda x: (x[0], x[1], len(x[2].schluessel)), reverse=True)
    return treffer[0][2]


def verhoerer_map(system: str | None, system_name: str | None = None) -> dict[str, str]:
    liste = erkennen(system, system_name)
    return {alt.casefold(): neu for alt, neu in (liste.verhoerer if liste else ())}


def im_kontext(term: str, texte) -> bool:
    n = _normal(term)
    if not n:
        return False
    return any(_phrase_enthalten(_normal(str(text or "")), n) for text in texte)


def promovierte_stufe_b(system: str | None, system_name: str | None, texte, limit: int = 24) -> tuple[str, ...]:
    """Stufe-B-Begriffe, die bereits in Kampagnentexten vorkommen, in Listenreihenfolge zurückgeben."""
    liste = erkennen(system, system_name)
    if liste is None:
        return ()
    aus = [term for term in liste.stufe_b if im_kontext(term, texte)]
    return tuple(aus[:max(0, limit)])
