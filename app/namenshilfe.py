"""Namenshilfe für die Transkription.

Whisper bekommt nur einen kleinen, priorisierten Hotword-Vorspann:
- manuell korrigierte/ergänzte Kampagnenbegriffe,
- wichtige Namen der Kampagne,
- Stufe-B-Systembegriffe, die in der Kampagne tatsächlich vorkommen,
- die kurze Stufe A des Spielsystems.

Die große Stufe B wird nicht pauschal an Whisper geschickt. Sie dient dem
Wörterbuch und wird nur kontextabhängig hochgestuft. Charakter-Hintergründe,
SL-Notizen und gmNotes werden dabei nur zur Relevanzerkennung benutzt und nie
selbst als Hotword-Text an Whisper übergeben.
"""
import json
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import systembegriffe as begriffslisten
from app.models import Campaign, Entry, EntryMention, GameSession, Member, User

MAX_BEGRIFFE = 80
MAX_ZEICHEN = 700  # grob 200 Token – mehr schneidet Whisper ohnehin ab
MAX_PROMOVIERT = 24


@lru_cache(maxsize=64)
def systembegriffe(system: str | None, system_name: str | None = None) -> tuple[str, ...]:
    """Nur Stufe A – die lange Stufe B wird niemals pauschal zu Whisper-Hotwords."""
    liste = begriffslisten.erkennen(system, system_name)
    return liste.stufe_a if liste else ()


MAX_ANZEIGE = 200   # Schnittstelle 0.4.6: höchstens 200 Einträge …
MAX_LAENGE = 40     # … à 40 Zeichen


def _gespeichert(campaign: Campaign) -> dict:
    try:
        d = json.loads(campaign.namenshilfe or "{}")
    except ValueError:
        d = {}
    return {k: [str(x) for x in d.get(k) or [] if str(x).strip()] for k in ("extra", "entfernt", "ignoriert")}


def _speichern(campaign: Campaign, d: dict) -> None:
    campaign.namenshilfe = json.dumps(d, ensure_ascii=False)


def _eindeutig(namen) -> list[str]:
    aus, gesehen = [], set()
    for n in namen:
        n = " ".join(str(n or "").split())[:MAX_LAENGE]
        if n and n.casefold() not in gesehen:
            gesehen.add(n.casefold())
            aus.append(n)
    return aus


def abgeleitet(db: Session, campaign: Campaign) -> list[str]:
    """Namen aus der Kampagne selbst: Charaktere und Anzeigenamen, Bibel (zuletzt erwähnte zuerst), Systemname."""
    namen: list[str] = []
    for m, u in db.execute(select(Member, User).join(User, User.id == Member.user_id)
                           .where(Member.campaign_id == campaign.id)).all():
        namen += [m.character_name, u.display_name]
    letzte = (select(EntryMention.entry_id, func.max(GameSession.number).label("nr"))
              .join(GameSession, GameSession.id == EntryMention.session_id)
              .group_by(EntryMention.entry_id).subquery())
    for e, _ in db.execute(
        select(Entry, letzte.c.nr).outerjoin(letzte, letzte.c.entry_id == Entry.id)
        .where(Entry.campaign_id == campaign.id)
        .order_by(letzte.c.nr.desc().nulls_last(), Entry.updated_at.desc())
    ).all():
        namen.append(e.name)
    namen.append(campaign.system_name)
    return _eindeutig(namen)


def anzeige(db: Session, campaign: Campaign) -> list[str]:
    """Campaign.hotwords für die SL: Eigenes und Korrigiertes zuerst, dann die abgeleiteten Namen ohne die, die die
    SL herausgenommen hat. Systemlisten gehören bewusst nicht in diese editierbare Anzeige."""
    d = _gespeichert(campaign)
    raus = {x.casefold() for x in d["entfernt"]}
    return _eindeutig(d["extra"] + [n for n in abgeleitet(db, campaign) if n.casefold() not in raus])[:MAX_ANZEIGE]


def setzen(db: Session, campaign: Campaign, liste: list[str]) -> None:
    """PATCH hotwords: Liste ganz ersetzen. Gespeichert wird nur die Abweichung von den abgeleiteten Namen – neue
    Bibel-Einträge kommen so weiter von selbst dazu."""
    neu = _eindeutig(liste)[:MAX_ANZEIGE]
    basis = abgeleitet(db, campaign)
    neu_klein = {n.casefold() for n in neu}
    basis_klein = {n.casefold() for n in basis}
    d = _gespeichert(campaign)
    d["extra"] = [n for n in neu if n.casefold() not in basis_klein]
    d["entfernt"] = [n for n in basis if n.casefold() not in neu_klein]
    _speichern(campaign, d)


def hinzufuegen(db: Session, campaign: Campaign, wort: str) -> None:
    """Korrektur mit addToHotwords: vorne einreihen (wichtiger als Abgeleitetes)."""
    wort = " ".join(wort.split())[:MAX_LAENGE]
    if not wort:
        return
    d = _gespeichert(campaign)
    d["entfernt"] = [n for n in d["entfernt"] if n.casefold() != wort.casefold()]
    d["extra"] = _eindeutig([wort] + d["extra"])[:MAX_ANZEIGE]
    _speichern(campaign, d)


def ignorieren(campaign: Campaign, wort: str) -> None:
    """Korrektur mit leerem correct: Begriff so lassen, nicht mehr als unsicher melden."""
    d = _gespeichert(campaign)
    d["ignoriert"] = _eindeutig(d["ignoriert"] + [wort])[-500:]
    _speichern(campaign, d)


def ignoriert(campaign: Campaign) -> set[str]:
    return {x.casefold() for x in _gespeichert(campaign)["ignoriert"]}


def _kontexttexte(db: Session, campaign: Campaign) -> list[str]:
    """Texte nur zur Auswahl relevanter Stufe-B-Begriffe; sie werden nie selbst an Whisper geschickt."""
    texte = [campaign.title, campaign.description, campaign.world_info, campaign.system_name]
    for e in db.scalars(select(Entry).where(Entry.campaign_id == campaign.id)):
        texte += [e.name, e.summary, e.gm_notes]
    for m in db.scalars(select(Member).where(Member.campaign_id == campaign.id)):
        texte += [m.character_name, m.character_nickname, m.character_summary, m.character_backstory]
    return [str(t) for t in texte if t]


def _packen(gruppen: list[list[str]]) -> list[str]:
    ergebnis: list[str] = []
    gesehen: set[str] = set()
    laenge = 0
    for gruppe in gruppen:
        for roh in gruppe:
            n = " ".join(str(roh or "").split())[:MAX_LAENGE]
            k = n.casefold()
            if not n or k in gesehen:
                continue
            zusaetzlich = len(n) + (2 if ergebnis else 0)
            if len(ergebnis) >= MAX_BEGRIFFE or laenge + zusaetzlich > MAX_ZEICHEN:
                continue
            ergebnis.append(n)
            gesehen.add(k)
            laenge += zusaetzlich
    return ergebnis


def fuer_kampagne(db: Session, campaign: Campaign) -> list[str]:
    """Priorisierte Whisper-Hotwords innerhalb des festen Budgets.

    Reihenfolge:
    1. manuell ergänzte/korrigierte Begriffe,
    2. die wichtigsten aktuellen Kampagnennamen,
    3. in Kampagnentexten gefundene Begriffe aus Stufe B,
    4. Stufe A des Systems,
    5. übrige Kampagnennamen.
    """
    d = _gespeichert(campaign)
    raus = {x.casefold() for x in d["entfernt"]}
    basis = [n for n in abgeleitet(db, campaign) if n.casefold() not in raus]
    wichtig, rest = basis[:12], basis[12:]
    promoviert = list(begriffslisten.promovierte_stufe_b(
        campaign.system, campaign.system_name, _kontexttexte(db, campaign), limit=MAX_PROMOVIERT
    ))
    stufe_a = list(systembegriffe(campaign.system, campaign.system_name))
    return _packen([d["extra"], wichtig, promoviert, stufe_a, rest])
