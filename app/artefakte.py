"""Artefakte aus Modelltexten deterministisch entfernen (Kapitel und Vorschläge).

Keine Prompt-Schleife, sondern feste Regeln nach dem Schreiben – sie gelten für jedes Modell (lokal und Cloud):

- Sätze in der Du-/Ihr-Form außerhalb von Anführungszeichen: meist wörtlich abgeschriebene Rede der Spielleitung
  („Ihr erinnert euch …“, „Und sobald ihr zu etwas Geld gekommen seid …“). Satz fällt weg.
- Regelsprache (Lebenspunkte, Proben, Würfe …): der Satzteil fällt weg, bleibt zu wenig übrig, der ganze Satz.
- Kraftausdrücke aus dem Gespräch am Tisch: Satz fällt weg.
- Namen der Menschen am Tisch: Spielernamen werden durch den Namen ihrer Figur ersetzt; die Spielleitung oder ein
  Spieler ohne Figur hat im Kapitel nichts verloren (Satz fällt weg). Namen, die zugleich Figur oder Bibel-Eintrag
  sind, bleiben unberührt.
- Wiederholte Sätze (nahezu wortgleich, z. B. aus einer Ergänzung): der spätere fällt weg.
- Versalien in Eigennamen („BANBALADIN“) → normale Schreibung; „den Spielern“ → „der Gruppe“.

Jede Änderung wird als Befund zurückgegeben (Art und gekürzter Text), damit der Probelauf zeigt, was entfernt wurde.
Die Abschrift selbst wird nie verändert – nur das, was das Modell daraus geschrieben hat.
"""
from __future__ import annotations

import re

_SATZ = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][\"“”»«]))\s+(?=[„\"»«(A-ZÄÖÜ0-9])")
_ZITAT = re.compile(r"„[^“”\"]*[“”\"]|\"[^\"]*\"|»[^«]*«|«[^»]*»")
_DU_FORM = re.compile(
    r"\b(?:euch|euer|eure[mnrs]?|du|dich|dir|dein(?:e[mnrs]?)?"
    r"|ihr (?:seid|habt|werdet|könnt|müsst|dürft|sollt|wollt|erinnert|seht|hört|wisst|kommt|geht|steht|merkt|"
    r"spürt|findet|bekommt|kriegt|lauft|fallt|liegt|sitzt|wacht|habt)"
    r"|(?:seid|habt|werdet|könnt|müsst|wollt|wisst|seht|hört) ihr"
    r"|seid|habt|werdet|könnt|müsst|dürft|sollt|wollt|wisst)\b", re.I)
_REGEL = re.compile(
    r"\b(?:\w*[Ll]ebenspunkt\w*|\w*[Kk]armapunkt\w*|\w*[Aa]stralpunkt\w*|\w*[Zz]auberpunkt\w*|\w*[Ss]chadenspunkt\w*"
    r"|Trefferpunkt\w*|Erfahrungspunkt\w*|Abenteuerpunkt\w*|Schicksalspunkt\w*|LeP|KaP|AsP|QS ?\d|Qualitätsstufe\w*"
    r"|\d+ ?[wWdD]\d+|[wW]20|gewürfelt|würfelt\w*|Würfelwurf\w*|Patzer\w*|Rettungswurf\w*|Probe (?:auf|gegen)"
    r"|(?:plus|minus) (?:eins|zwei|drei|\d+) (?:auf|Schmerz)\w*)\b")
_FLUCH = re.compile(r"\b(?:scheiß\w*|Scheiße|Arschloch|Wichser|Hurensohn|Kacke|fick\w*|Fick\w*|Fotze)\b", re.I)
_VERSAL = re.compile(r"\b([A-ZÄÖÜ]{4,})\b")
_ROEMISCH = re.compile(r"^[IVXLCDM]+$")
_TRENNER = re.compile(r",\s+|;\s+|\s+(?:und|sowie|doch|aber)\s+")


def _ohne_zitate(satz: str) -> str:
    return _ZITAT.sub(" ", satz)


_AUF = "„\"»"
_ZU = {"„": "“”\"", "\"": "\"", "»": "«"}


def _maske(absatz: str) -> str:
    """Absatz mit überdeckter wörtlicher Rede (gleich lang). Eine nicht geschlossene Rede reicht bis zum Absatzende –
    Modelle vergessen das schließende Zeichen, und Rede über mehrere Sätze wird sonst satzweise falsch beurteilt."""
    aus, offen = [], None
    for ch in absatz:
        if offen is None:
            if ch in _AUF:
                offen = ch
                aus.append(" ")
            else:
                aus.append(ch)
        else:
            aus.append(" ")
            if ch in _ZU[offen]:
                offen = None
    return "".join(aus)


def _saetze(absatz: str) -> list[tuple[str, str]]:
    """[(Satz, Satz ohne wörtliche Rede)]"""
    maske = _maske(absatz)
    aus, start = [], 0
    for m in _SATZ.finditer(absatz):
        aus.append((absatz[start:m.start()].strip(), maske[start:m.start()]))
        start = m.end()
    aus.append((absatz[start:].strip(), maske[start:]))
    return [(s, m) for s, m in aus if s]


def _woerter(t: str) -> list[str]:
    return [w for w in re.findall(r"\w+", t.casefold()) if len(w) > 2]


def _teil_entfernen(satz: str, m: re.Match) -> str:
    """Den Satzteil um den Treffer entfernen (zwischen Komma/„und“ links und Komma/Satzende rechts)."""
    links = 0
    for t in _TRENNER.finditer(satz, 0, m.start()):
        links = t.start()
    rechts_m = re.compile(r",\s+|;\s+").search(satz, m.end())
    rechts = rechts_m.start() if rechts_m else len(satz.rstrip(".!?…"))
    rest = (satz[:links] + satz[rechts:]).strip()
    rest = re.sub(r"\s+([,.;!?])", r"\1", rest)
    rest = re.sub(r"^[,;]\s*", "", rest)
    if rest[:1].islower():
        rest = rest[0].upper() + rest[1:]
    if rest and rest[-1] not in ".!?…“\"»":
        rest += "."
    return rest


def _namen_ersetzen(satz: str, personen: list[dict], geschuetzt: set[str]) -> tuple[str | None, bool]:
    """Spielername → Figurenname; Spielleitung oder Spieler ohne Figur → Satz weg (None)."""
    geaendert = False
    for p in personen:
        name = (p.get("name") or "").strip()
        if len(name) < 3:
            continue
        kandidaten = {name}
        vorname = name.split()[0]
        if len(vorname) >= 4:
            kandidaten.add(vorname)
        for k in sorted(kandidaten, key=len, reverse=True):
            if k.casefold() in geschuetzt:
                continue
            muster = re.compile(rf"(?<![\w-]){re.escape(k)}(?![\w-])")
            if not muster.search(satz):
                continue
            figur = (p.get("charakter") or "").strip()
            if p.get("rolle") == "gm" or not figur:
                return None, True
            satz = muster.sub(figur, satz)
            geaendert = True
    return satz, geaendert


def kapitel(text: str, personen: list[dict] | None = None, geschuetzte_namen: list[str] | None = None
            ) -> tuple[str, list[dict]]:
    """Kapiteltext säubern. Liefert (Text, Befunde)."""
    personen = personen or []
    geschuetzt = {n.casefold() for n in (geschuetzte_namen or []) if n}
    for p in personen:
        if p.get("charakter"):
            geschuetzt.add(p["charakter"].casefold())
            geschuetzt.update(w.casefold() for w in p["charakter"].split() if len(w) >= 3)
    befunde: list[dict] = []
    gesehen: list[set[str]] = []
    absaetze_aus = []
    for absatz in re.split(r"\n\s*\n", text or ""):
        saetze_aus = []
        for satz, draussen in _saetze(absatz.strip()):
            if _DU_FORM.search(draussen):
                befunde.append({"art": "du_form", "text": satz[:200]})
                continue
            if _FLUCH.search(satz):
                befunde.append({"art": "kraftausdruck", "text": satz[:200]})
                continue
            for _ in range(3):  # höchstens drei Regelstellen je Satz
                m = _REGEL.search(satz)
                if not m:
                    break
                neu = _teil_entfernen(satz, m)
                befunde.append({"art": "regel", "text": satz[:200]})
                satz = neu if len(_woerter(neu)) >= 3 else ""
                if not satz:
                    break
            if not satz:
                continue
            satz_neu, geaendert = _namen_ersetzen(satz, personen, geschuetzt)
            if satz_neu is None:
                befunde.append({"art": "name_am_tisch", "text": satz[:200]})
                continue
            if geaendert:
                befunde.append({"art": "name_ersetzt", "text": satz[:200]})
            satz = satz_neu
            w = set(_woerter(satz))
            if len(w) >= 6 and any(len(w & alt) >= 0.8 * len(w) for alt in gesehen):
                befunde.append({"art": "wiederholung", "text": satz[:200]})
                continue
            gesehen.append(w)
            saetze_aus.append(satz)
        if saetze_aus:
            absaetze_aus.append(" ".join(saetze_aus))
    aus = feinschliff("\n\n".join(absaetze_aus))
    return aus, befunde


def feinschliff(t: str) -> str:
    """Versalien und Tischsprache, die überall gilt (Kapitel, Fäden, Vorschläge)."""
    if not t:
        return t
    t = _VERSAL.sub(lambda m: m.group(1) if _ROEMISCH.match(m.group(1)) else m.group(1).capitalize(), t)
    t = re.sub(r"\bden Spielern\b", "der Gruppe", t)
    t = re.sub(r"\bden Spielercharakteren\b", "der Gruppe", t)
    t = re.sub(r"\bden Spielerinnen und Spielern\b", "der Gruppe", t)
    return t


def vorschlag(v: dict) -> dict:
    """Vorschlag säubern: Tischsprache und Versalien, Sätze in der Du-Form oder mit Kraftausdrücken weg."""
    for feld in ("title", "detail", "gmNotes"):
        wert = v.get(feld)
        if not isinstance(wert, str) or not wert:
            continue
        if feld != "title":
            zeilen = []
            for zeile in wert.split("\n"):
                saetze = [s for s, d in _saetze(zeile) if not _DU_FORM.search(d) and not _FLUCH.search(s)]
                zeilen.append(" ".join(saetze))
            wert = "\n".join(z for z in zeilen if z.strip())
        v[feld] = feinschliff(wert)
    return v
