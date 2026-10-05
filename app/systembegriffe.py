"""Strukturierte Rollenspiel-Begriffslisten für Whisper und die Namensprüfung.

Dateiformat (rückwärtskompatibel zu den alten einfachen Listen):

    # Erkennungsnamen: dsa, das schwarze auge
    # Stufe A
    Aventurien
    # Stufe A [de]
    Rüstungsklasse
    # Stufe A [en]
    Armor Class
    # Stufe B
    Borbarad
    # Verhörer
    Aventurin => Aventurien
    # Verhörer [de]
    Tschummer => Chummer

Ohne Sprachmarkierung ist ein Abschnitt SHARED: Er gilt unabhängig von der
gesprochenen Sprache. Das ist absichtlich so, weil deutschsprachige Runden
häufig englische Regelbegriffe und Eigennamen verwenden. Sprach-Overlays
ergänzen Shared, sie ersetzen es nicht.

Stufe A ist der kleine Systemkern für Whisper. Stufe B ist das große
Nachschlagewörterbuch und wird nur dann zum Hotword promoviert, wenn ein Begriff
im Kampagnenkontext vorkommt. Verhörer sind Korrekturhinweise, keine automatische
Textersetzung.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ORDNER = Path(__file__).resolve().parent / "begriffe"
_MAX_SYSTEM_DATEIEN = 100
_MEHRDEUTIGE_ALIASE = {"genesys", "imperium"}

_HEADER_ALIASE = re.compile(r"^#\s*Erkennungsnamen\s*:\s*(.+)$", re.IGNORECASE)
_HEADER_A = re.compile(r"^#\s*Stufe\s+A(?:\s*\[([a-z]{2}(?:-[a-z0-9]{2,8})?)\])?", re.IGNORECASE)
_HEADER_B = re.compile(r"^#\s*Stufe\s+B(?:\s*\[([a-z]{2}(?:-[a-z0-9]{2,8})?)\])?", re.IGNORECASE)
_HEADER_VERHOERER = re.compile(r"^#\s*Verhörer(?:\s*\[([a-z]{2}(?:-[a-z0-9]{2,8})?)\])?", re.IGNORECASE)


def _sprache(code: str | None) -> str | None:
    if not code:
        return None
    return code.casefold().replace("_", "-").split("-", 1)[0]


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


def _sprach_tupel(d: dict[str, list[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((k, _eindeutig(v)) for k, v in sorted(d.items()))


def _verhoerer_tupel(d: dict[str, list[tuple[str, str]]]) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    return tuple((k, tuple(v)) for k, v in sorted(d.items()))


@dataclass(frozen=True)
class Begriffsliste:
    schluessel: str
    aliases: tuple[str, ...]
    stufe_a: tuple[str, ...]
    stufe_b: tuple[str, ...]
    verhoerer: tuple[tuple[str, str], ...]
    stufe_a_sprachen: tuple[tuple[str, tuple[str, ...]], ...] = ()
    stufe_b_sprachen: tuple[tuple[str, tuple[str, ...]], ...] = ()
    verhoerer_sprachen: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()

    def a_overlay(self, sprache: str | None) -> tuple[str, ...]:
        return dict(self.stufe_a_sprachen).get(_sprache(sprache) or "", ())

    def b_overlay(self, sprache: str | None) -> tuple[str, ...]:
        return dict(self.stufe_b_sprachen).get(_sprache(sprache) or "", ())

    def a_fuer(self, sprache: str | None) -> tuple[str, ...]:
        return _eindeutig(self.stufe_a + self.a_overlay(sprache))

    def b_fuer(self, sprache: str | None) -> tuple[str, ...]:
        a = {x.casefold() for x in self.a_fuer(sprache)}
        return tuple(x for x in _eindeutig(self.stufe_b + self.b_overlay(sprache)) if x.casefold() not in a)

    def verhoerer_fuer(self, sprache: str | None) -> tuple[tuple[str, str], ...]:
        werte: dict[str, tuple[str, str]] = {}
        for alt, neu in self.verhoerer:
            werte[alt.casefold()] = (alt, neu)
        for alt, neu in dict(self.verhoerer_sprachen).get(_sprache(sprache) or "", ()):
            werte[alt.casefold()] = (alt, neu)
        return tuple(werte.values())

    @property
    def alle(self) -> tuple[str, ...]:
        """Shared-Wörterbuch für rückwärtskompatible Aufrufer."""
        return self.stufe_a + self.stufe_b

    def alle_fuer(self, sprache: str | None) -> tuple[str, ...]:
        return self.a_fuer(sprache) + self.b_fuer(sprache)

    def fingerprint(self, sprache: str | None = None) -> str:
        payload = {
            "format": 2,
            "system": self.schluessel,
            "language": _sprache(sprache),
            "a": self.a_fuer(sprache),
            "b": self.b_fuer(sprache),
            "mishearings": self.verhoerer_fuer(sprache),
        }
        roh = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(roh).hexdigest()


@lru_cache(maxsize=_MAX_SYSTEM_DATEIEN)
def _lesen_pfad(pfad: str) -> Begriffsliste:
    p = Path(pfad)
    abschnitt = "a"
    sprache: str | None = None
    aliases: list[str] = []
    a: list[str] = []
    b: list[str] = []
    verhoerer: list[tuple[str, str]] = []
    a_sprachen: dict[str, list[str]] = {}
    b_sprachen: dict[str, list[str]] = {}
    v_sprachen: dict[str, list[tuple[str, str]]] = {}

    for roh in p.read_text(encoding="utf-8").splitlines():
        zeile = roh.strip()
        if not zeile:
            continue
        m = _HEADER_ALIASE.match(zeile)
        if m:
            aliases.extend(x.strip() for x in m.group(1).split(",") if x.strip())
            continue
        m = _HEADER_A.match(zeile)
        if m:
            abschnitt, sprache = "a", _sprache(m.group(1))
            continue
        m = _HEADER_B.match(zeile)
        if m:
            abschnitt, sprache = "b", _sprache(m.group(1))
            continue
        m = _HEADER_VERHOERER.match(zeile)
        if m:
            abschnitt, sprache = "verhoerer", _sprache(m.group(1))
            continue
        if zeile.startswith("#"):
            continue

        if abschnitt == "verhoerer":
            if "=>" not in zeile:
                continue
            alt, neu = (x.strip() for x in zeile.split("=>", 1))
            if alt and neu and alt.casefold() != neu.casefold():
                (v_sprachen.setdefault(sprache, []) if sprache else verhoerer).append((alt, neu))
        elif abschnitt == "b":
            (b_sprachen.setdefault(sprache, []) if sprache else b).append(zeile)
        else:
            (a_sprachen.setdefault(sprache, []) if sprache else a).append(zeile)

    stufe_a = _eindeutig(a)
    shared_a = {x.casefold() for x in stufe_a}
    stufe_b = tuple(x for x in _eindeutig(b) if x.casefold() not in shared_a)

    a_norm: dict[str, list[str]] = {}
    b_norm: dict[str, list[str]] = {}
    shared_b = {x.casefold() for x in stufe_b}
    for lang, werte in a_sprachen.items():
        a_norm[lang] = [x for x in _eindeutig(werte) if x.casefold() not in shared_a]
    for lang, werte in b_sprachen.items():
        a_keys = shared_a | {x.casefold() for x in a_norm.get(lang, [])}
        b_norm[lang] = [x for x in _eindeutig(werte)
                        if x.casefold() not in a_keys and x.casefold() not in shared_b]

    return Begriffsliste(
        schluessel=p.stem,
        aliases=_eindeutig(aliases),
        stufe_a=stufe_a,
        stufe_b=stufe_b,
        verhoerer=tuple(verhoerer),
        stufe_a_sprachen=_sprach_tupel(a_norm),
        stufe_b_sprachen=_sprach_tupel(b_norm),
        verhoerer_sprachen=_verhoerer_tupel(v_sprachen),
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
    return " ".join(re.findall(r"[^\W_]+", (text or "").casefold(), flags=re.UNICODE))


def _phrase_enthalten(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def erkennen(system: str | None, system_name: str | None = None) -> Begriffsliste | None:
    """Direkten Systemschlüssel nutzen; bei other den freien Systemnamen anhand der Aliaszeile erkennen."""
    direkt = lesen(system)
    if direkt is not None:
        return direkt

    name = _normal(system_name)
    if not name:
        return None

    if any(x in name.split() for x in ("40k", "40000")) or _phrase_enthalten(name, "40 000") or \
            _phrase_enthalten(name, "dark heresy") or _phrase_enthalten(name, "wrath glory") or \
            _phrase_enthalten(name, "rogue trader"):
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


def verhoerer_map(system: str | None, system_name: str | None = None, sprache: str | None = None) -> dict[str, str]:
    liste = erkennen(system, system_name)
    return {alt.casefold(): neu for alt, neu in (liste.verhoerer_fuer(sprache) if liste else ())}


def im_kontext(term: str, texte) -> bool:
    n = _normal(term)
    if not n:
        return False
    return any(_phrase_enthalten(_normal(str(text or "")), n) for text in texte)


def promovierte_stufe_b(
    system: str | None, system_name: str | None, texte, limit: int = 24, sprache: str | None = None
) -> tuple[str, ...]:
    """Stufe-B-Begriffe, die bereits im Kampagnenkontext vorkommen, in Listenreihenfolge."""
    liste = erkennen(system, system_name)
    if liste is None:
        return ()
    aus = [term for term in liste.b_fuer(sprache) if im_kontext(term, texte)]
    return tuple(aus[:max(0, limit)])
