"""Wörterbücher für die Prüfung unsicher erkannter Namen (app/unsicher.py).

Drei Quellen sagen, ob ein Wort „bekannt“ ist und deshalb nicht als unsicherer Name gemeldet wird:

1. **Allgemeine Wortliste** je Sprache (de, en): Häufigkeitslisten gesprochener Sprache aus Filmuntertiteln
   (OpenSubtitles 2018, aufbereitet von Hermit Dave, https://github.com/hermitdave/FrequencyWords, Inhalt unter
   CC BY-SA 4.0). Gesprochene Sprache passt zu Transkripten, und die Listen enthalten gebeugte Formen („Schwertes“).
   Die Listen sind zu groß fürs Repo: Die Wartung lädt sie einmal von einer festen Fassung (Prüfsumme), behält Wörter,
   die mindestens MIN_ANZAHL-mal vorkommen, und legt sie verkleinert unter <data>/woerterbuch/ ab. Ohne Netz bleibt
   die Prüfung ohne diese Quelle – nichts bricht.
   Neue Fassung der Listen: Mit einem Server-Update ändern sich FASSUNG und Prüfsummen; die Wartung merkt am
   Stand-Vermerk (<sprache>.stand), dass die abgelegte Liste nicht mehr passt, und lädt neu. Bis dahin bleibt die
   alte Liste in Gebrauch. Ungeprüft „das Neueste“ von GitHub zu laden, ist bewusst nicht vorgesehen.
   Zusammengesetzte deutsche Wörter („Plattenpanzer“, „Fahndungsplakat“) gelten als bekannt, wenn beide Teile
   bekannt sind (mit Fugen-s/-n/-en/-es/-e/-er).
2. **Texte der Kampagne:** Welt-Info, Bibel (auch geheime Einträge und gmNotes), Charakterbeschreibungen und
   -hintergründe, SL-Notizen der Kapitel, ausgelesene SL-Unterlagen. Was dort geschrieben steht, ist eine bekannte
   Schreibweise. Großgeschriebene Wörter daraus, die kein allgemeines Wort sind, dienen außerdem als Vorschläge
   („Meintest du …“). Das Ergebnis sieht nur die SL; in Recaps oder Vorschläge fließt davon nichts.
3. **Was die Runde schon gesagt hat:** Wörter, die in anderen Kapiteln der Kampagne mehrfach sicher erkannt wurden
   (nicht in der Liste der unsicheren Wörter standen).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("woerterbuch")

FASSUNG = "525f9b560de45753a5ea01069454e72e9aa541c6"  # FrequencyWords, fest – neue Fassung nur bewusst
QUELLEN = {
    "de": ("content/2018/de/de_full.txt", "a5fc13f4efaaf70fd9226d11295fd3cd4c2264852ee1fad22c0abaf64acec8dc"),
    "en": ("content/2018/en/en_full.txt", "7fea67ab954e2c01df6c608c9826e594cf36f8823b3243554f88245fb75dc506"),
}
MIN_ANZAHL = 10          # seltener vorkommende Einträge sind oft Namen oder Tippfehler aus Untertiteln
NEUER_VERSUCH_S = 3600   # nach einem gescheiterten Download frühestens wieder
FUGEN = ("", "s", "es", "n", "en", "e", "er")
MIN_TEIL = 3
MIN_SICHER_GEHOERT = 3   # so oft in anderen Kapiteln sicher erkannt → bekannt

_TOKEN = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)
_letzter_versuch: dict[str, float] = {}


def ordner() -> Path:
    from app.config import get_settings

    return Path(get_settings().data_dir) / "woerterbuch"


def pfad(sprache: str) -> Path:
    return ordner() / f"{sprache}.txt.gz"


def woerter(text: str | None) -> list[str]:
    return _TOKEN.findall(text or "")


# ---------------------------------------------------------------- allgemeine Wortliste
@lru_cache(maxsize=4)
def _laden(datei: str, _stand: float) -> frozenset[str]:
    with gzip.open(datei, "rt", encoding="utf-8") as f:
        return frozenset(z.strip() for z in f if z.strip())


def liste(sprache: str) -> frozenset[str]:
    """Allgemeine Wörter (klein) – leer, solange die Liste noch nicht geladen ist."""
    p = pfad(sprache)
    try:
        return _laden(str(p), p.stat().st_mtime_ns)
    except (OSError, EOFError):
        return frozenset()


def bereit(sprache: str) -> bool:
    """Liste liegt vor und ist benutzbar (vielleicht noch in einer älteren Fassung)."""
    return pfad(sprache).is_file()


def _soll(sprache: str) -> str:
    return f"{FASSUNG} {QUELLEN[sprache][1]} {MIN_ANZAHL}"


def _stand_pfad(sprache: str) -> Path:
    return ordner() / f"{sprache}.stand"


def aktuell(sprache: str) -> bool:
    """Liste liegt in genau der Fassung vor, die dieser Server erwartet."""
    if not bereit(sprache):
        return False
    try:
        return _stand_pfad(sprache).read_text(encoding="utf-8").strip() == _soll(sprache)
    except OSError:
        pass
    # Listen aus Server 0.4.30 haben noch keinen Stand-Vermerk – gleiche Fassung laut QUELLE.txt: nachtragen
    try:
        if FASSUNG in (ordner() / "QUELLE.txt").read_text(encoding="utf-8"):
            _stand_pfad(sprache).write_text(_soll(sprache), encoding="utf-8")
            return True
    except OSError:
        pass
    return False


def herunterladen(sprache: str, klient=None) -> bool:
    """Liste laden, Prüfsumme prüfen, auf häufige Wörter kürzen und verkleinert ablegen."""
    import httpx

    if sprache not in QUELLEN:
        return False
    datei, sha = QUELLEN[sprache]
    url = f"https://raw.githubusercontent.com/hermitdave/FrequencyWords/{FASSUNG}/{datei}"
    eigen = klient is None
    klient = klient or httpx.Client(timeout=httpx.Timeout(30, read=300), follow_redirects=True)
    try:
        r = klient.get(url)
        r.raise_for_status()
        roh = r.content
    finally:
        if eigen:
            klient.close()
    if hashlib.sha256(roh).hexdigest() != sha:
        raise ValueError(f"Prüfsumme der Wortliste {sprache} stimmt nicht")
    behalten = []
    for zeile in roh.decode("utf-8", "replace").splitlines():
        teile = zeile.split()
        if len(teile) == 2 and teile[1].isdigit() and int(teile[1]) >= MIN_ANZAHL:
            behalten.append(teile[0].casefold())
    ordner().mkdir(parents=True, exist_ok=True)
    neu = pfad(sprache).with_suffix(".tmp")
    with gzip.open(neu, "wt", encoding="utf-8") as f:
        f.write("\n".join(sorted(set(behalten))))
    neu.replace(pfad(sprache))
    _stand_pfad(sprache).write_text(_soll(sprache), encoding="utf-8")
    (ordner() / "QUELLE.txt").write_text(
        "Wortlisten: FrequencyWords von Hermit Dave (https://github.com/hermitdave/FrequencyWords), Fassung "
        f"{FASSUNG}, aus OpenSubtitles 2018. Lizenz: CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). "
        f"Gekürzt auf Wörter mit mindestens {MIN_ANZAHL} Vorkommen, kleingeschrieben.\n", encoding="utf-8")
    log.info("Wortliste %s geladen: %d Wörter", sprache, len(set(behalten)))
    return True


def automatisch(sprachen=("de", "en")) -> None:
    """Wartung: fehlende oder veraltete Wortlisten laden – nach einem Fehlschlag frühestens nach einer Stunde wieder."""
    for sprache in sprachen:
        if aktuell(sprache) or time.monotonic() - _letzter_versuch.get(sprache, -1e9) < NEUER_VERSUCH_S:
            continue
        _letzter_versuch[sprache] = time.monotonic()
        try:
            herunterladen(sprache)
            _letzter_versuch.pop(sprache, None)  # Wartezeit gilt nur nach Fehlschlägen
        except Exception as e:  # noqa: BLE001 – ohne Liste geht es auch, nur ungenauer
            log.warning("Wortliste %s nicht geladen: %s", sprache, e)


def _zusammengesetzt(wort: str, bekannt) -> bool:
    """Deutsches Kompositum aus zwei bekannten Teilen (mit Fugenlaut)?"""
    for i in range(MIN_TEIL, len(wort) - MIN_TEIL + 1):
        vorn, hinten = wort[:i], wort[i:]
        if not bekannt(hinten):
            continue
        for fuge in FUGEN:
            if fuge and vorn.endswith(fuge) and len(vorn) - len(fuge) >= MIN_TEIL and bekannt(vorn[:-len(fuge)]):
                return True
            if not fuge and bekannt(vorn):
                return True
    return False


# ---------------------------------------------------------------- Kampagne
@dataclass
class Bekannt:
    """Alles, was für eine Kampagne als bekannte Schreibweise gilt."""
    sprache: str
    allgemein: frozenset[str] = frozenset()
    kampagne: set[str] = field(default_factory=set)   # klein
    namen: dict[str, str] = field(default_factory=dict)  # klein → Schreibweise; namenartige Wörter der Kampagne

    def _einfach(self, w: str) -> bool:
        return w in self.kampagne or w in self.allgemein

    def direkt(self, wort: str) -> bool:
        """Genau so bekannt (nicht erst über ein zusammengesetztes Wort)."""
        return self._einfach(wort.casefold())

    def enthaelt(self, wort: str) -> bool:
        w = wort.casefold()
        if self._einfach(w):
            return True
        if "-" in w:
            return all(self._einfach(t) or not t for t in w.split("-"))
        return self.sprache == "de" and len(w) >= 2 * MIN_TEIL and _zusammengesetzt(w, lambda t: t in self.allgemein)


def kampagne(db: Session, campaign, ausser_session_id: str | None = None) -> Bekannt:
    from app import namenshilfe, systembegriffe as begriffslisten, unterlagen
    from app.models import CampaignDocument, Entry, GameSession, GmNote, Member

    sprache = campaign.language if campaign.language in QUELLEN else "de"
    b = Bekannt(sprache=sprache, allgemein=liste(sprache))
    systemliste = begriffslisten.erkennen(campaign.system, campaign.system_name)
    if systemliste is not None:
        # Stufe B gehört ins Nachschlagewörterbuch, nicht pauschal in den Whisper-Prompt.
        # So werden korrekte Systembegriffe nicht als unbekannte Namen gemeldet und
        # ähnlich erkannte Schreibweisen können als Vorschlag auftauchen.
        for term in systemliste.alle:
            for w in woerter(term):
                b.kampagne.add(w.casefold())
            if len(term) >= 3 and term.casefold() not in b.allgemein:
                b.namen.setdefault(term.casefold(), term)
    texte: list[str] = [campaign.world_info or "", campaign.title or "", campaign.description or "",
                        campaign.system_name or ""]
    texte += namenshilfe.anzeige(db, campaign)
    for e in db.scalars(select(Entry).where(Entry.campaign_id == campaign.id)):
        texte += [e.name, e.summary or "", e.gm_notes or ""]
    for m in db.scalars(select(Member).where(Member.campaign_id == campaign.id)):
        texte += [m.character_name or "", m.character_summary or "", m.character_backstory or ""]
    sitzungen = list(db.scalars(select(GameSession.id).where(GameSession.campaign_id == campaign.id)))
    for n in db.scalars(select(GmNote).where(GmNote.session_id.in_(sitzungen))):
        texte.append(n.text or "")
    for doc in db.scalars(select(CampaignDocument).where(CampaignDocument.campaign_id == campaign.id)):
        try:
            texte += [a.get("text") or "" for a in unterlagen.text_laden(doc.id)]
        except Exception:  # noqa: BLE001 – Datei fehlt oder ist kaputt: dann ohne diese Unterlage
            continue
    for t in texte:
        for w in woerter(t):
            k = w.casefold()
            b.kampagne.add(k)
            if w[0].isupper() and len(w) >= 3 and k not in b.allgemein:
                b.namen.setdefault(k, w)
    b.kampagne |= gesagt(db, [s for s in sitzungen if s != ausser_session_id])
    return b


_gesagt_cache: dict[tuple, frozenset[str]] = {}


def gesagt(db: Session, sitzungen: list[str]) -> frozenset[str]:
    """Wörter, die in diesen Kapiteln mehrfach sicher erkannt wurden. Zwischengespeichert, solange sich an den
    Transkripten nichts ändert (Anzahl der Abschnitte als Kennzeichen)."""
    from sqlalchemy import func

    from app.models import TranscriptSegment

    if not sitzungen:
        return frozenset()
    anzahl = db.scalar(select(func.count()).select_from(TranscriptSegment)
                       .where(TranscriptSegment.session_id.in_(sitzungen)))
    schluessel = (tuple(sorted(sitzungen)), anzahl)
    if schluessel in _gesagt_cache:
        return _gesagt_cache[schluessel]
    zaehler: Counter[str] = Counter()
    for text, unsicher in db.execute(
        select(TranscriptSegment.text, TranscriptSegment.unsicher)
        .where(TranscriptSegment.session_id.in_(sitzungen))
    ).all():
        schwach = set()
        if unsicher:
            try:
                schwach = {str(x.get("word") or "").strip(".,;:!?…\"'„“”").casefold() for x in json.loads(unsicher)}
            except ValueError:
                schwach = set()
        for w in woerter(text):
            k = w.casefold()
            if k not in schwach:
                zaehler[k] += 1
    ergebnis = frozenset(k for k, n in zaehler.items() if n >= MIN_SICHER_GEHOERT)
    if len(_gesagt_cache) > 50:
        _gesagt_cache.clear()
    _gesagt_cache[schluessel] = ergebnis
    return ergebnis
