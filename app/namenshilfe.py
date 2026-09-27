"""Namenshilfe für die Transkription: Eigennamen aus der Kampagne plus – nur bei großen Systemen – eine
kleine Begriffsliste. Reihenfolge nach Wichtigkeit, weil Whisper nur einen begrenzten Vorspann nutzt.

Nie enthalten: Charakter-Hintergründe, SL-Notizen, gmNotes, Kommentare (nur Namen, keine Inhalte).
"""
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


def fuer_kampagne(db: Session, campaign: Campaign) -> list[str]:
    namen: list[str] = []

    def dazu(n: str | None) -> None:
        n = (n or "").strip()
        if n and n.casefold() not in {x.casefold() for x in namen}:
            namen.append(n)

    # 1. Charakter- und Anzeigenamen der Mitglieder
    for m, u in db.execute(select(Member, User).join(User, User.id == Member.user_id)
                           .where(Member.campaign_id == campaign.id)).all():
        dazu(m.character_name)
        dazu(u.display_name)
    # 2. Bibel: zuletzt erwähnte bzw. geänderte Einträge zuerst
    letzte = (select(EntryMention.entry_id, func.max(GameSession.number).label("nr"))
              .join(GameSession, GameSession.id == EntryMention.session_id)
              .group_by(EntryMention.entry_id).subquery())
    for e, _ in db.execute(
        select(Entry, letzte.c.nr).outerjoin(letzte, letzte.c.entry_id == Entry.id)
        .where(Entry.campaign_id == campaign.id)
        .order_by(letzte.c.nr.desc().nulls_last(), Entry.updated_at.desc())
    ).all():
        dazu(e.name)
    # 3. Freier Systemname und Begriffsliste des Systems
    dazu(campaign.system_name)
    for b in systembegriffe(campaign.system):
        dazu(b)
    # Begrenzen
    ergebnis, laenge = [], 0
    for n in namen[:MAX_BEGRIFFE]:
        if laenge + len(n) + 2 > MAX_ZEICHEN:
            break
        ergebnis.append(n)
        laenge += len(n) + 2
    return ergebnis
