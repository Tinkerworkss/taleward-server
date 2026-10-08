"""Namenshilfe für die Transkription: Eigennamen aus der Kampagne plus – nur bei großen Systemen – eine
kleine Begriffsliste. Reihenfolge nach Wichtigkeit, weil Whisper nur einen begrenzten Vorspann nutzt.

Nie enthalten: Charakter-Hintergründe, SL-Notizen, gmNotes, Kommentare (nur Namen, keine Inhalte).
"""
import json
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Campaign, Entry, EntryMention, GameSession, Member, User

MAX_BEGRIFFE = 80
MAX_ZEICHEN = 700  # grob 200 Token – mehr schneidet Whisper ohnehin ab

_ORDNER = Path(__file__).resolve().parent / "begriffe"


@lru_cache
def systembegriffe(system: str | None) -> tuple[str, ...]:
    if not system or system == "other":
        return ()
    datei = _ORDNER / f"{system}.txt"
    if not datei.exists():
        return ()
    zeilen = datei.read_text(encoding="utf-8").splitlines()
    return tuple(z.strip() for z in zeilen if z.strip() and not z.startswith("#"))


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
    SL herausgenommen hat."""
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


def fuer_kampagne(db: Session, campaign: Campaign, kapitel: int | None = None) -> list[str]:
    """Was der Worker als hotwords bekommt: Namen aus dem Kapitelplan für dieses Kapitel (0.4.12, nur die Wörter),
    dann die Namenshilfe der Kampagne, dann die Begriffsliste des Systems – begrenzt, weil Whisper nur einen kurzen
    Vorspann nutzt. Die Plan-Namen stehen vorn, weil die SL sie ausdrücklich für diese Runde eingetragen hat."""
    from app.routers.plaene import namen_fuer

    namen = _eindeutig(namen_fuer(db, campaign.id, kapitel) + anzeige(db, campaign)
                       + list(systembegriffe(campaign.system)))
    ergebnis, laenge = [], 0
    for n in namen[:MAX_BEGRIFFE]:
        if laenge + len(n) + 2 > MAX_ZEICHEN:
            break
        ergebnis.append(n)
        laenge += len(n) + 2
    return ergebnis
