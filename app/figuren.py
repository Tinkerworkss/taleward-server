"""Figuren ausgetretener Spieler (Schnittstelle 0.4.9).

Verlässt ein Spieler die Kampagne (oder löscht sein Konto), bleibt seine Figur als Geschichte in der Kampagne: der
pc-Eintrag mit Name, Kurzbeschreibung, Erwähnungen und Kapiteln. Die SL bekommt einen Hinweis (character_orphaned)
und entscheidet: als NSC weiterführen (to-npc) oder einem anderen Spieler geben (assign). Die Person selbst –
Hintergrund, Kommentare, Zustimmungen, Kennung des Charakters, Porträt – geht dabei nicht mit.

Der Hinweis räumt sich selbst ab, sobald keiner seiner Einträge mehr eine Figur ohne aktiven Halter ist (nach
to-npc, assign oder wenn die Person zurückkommt).
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.db import utcnow
from app.models import Entry, GmNotice, Member

HINWEIS = "character_orphaned"


def _halter_weg(db: Session, e: Entry) -> bool:
    """pc ohne aktiven Halter: kein Halter, Halter hat verlassen oder Konto gelöscht. Ein offener Platz aus einem
    Umzug zählt nicht als weg – der wartet auf seine Person."""
    if e.holder_member_id is None:
        return True
    h = db.get(Member, e.holder_member_id)
    return h is None or h.left_at is not None or h.deleted_at is not None


def verwaist_melden(db: Session, m: Member) -> None:
    """Beim Verlassen bzw. Kontolöschen: Hinweis an die SL mit den pc-Einträgen des Spielers (nur Rolle player)."""
    if m.role != "player":
        return
    ids = sorted(db.scalars(select(Entry.id).where(Entry.campaign_id == m.campaign_id, Entry.type == "pc",
                                                   Entry.holder_member_id == m.id)))
    if not ids:
        return
    vorhanden = db.scalar(select(GmNotice).where(GmNotice.campaign_id == m.campaign_id, GmNotice.code == HINWEIS,
                                                 GmNotice.member_id == m.id))
    if vorhanden is not None:  # z. B. erst verlassen, später Konto gelöscht
        vorhanden.entry_ids = json.dumps(sorted(set(json.loads(vorhanden.entry_ids or "[]")) | set(ids)))
        return
    db.add(GmNotice(campaign_id=m.campaign_id, code=HINWEIS, member_id=m.id, entry_ids=json.dumps(ids)))


def aufraeumen(db: Session, campaign_id: str) -> None:
    """Hinweise entfernen, deren Einträge alle nicht mehr pc ohne aktiven Halter sind."""
    db.flush()
    for n in db.scalars(select(GmNotice).where(GmNotice.campaign_id == campaign_id, GmNotice.code == HINWEIS)):
        offen = False
        for i in json.loads(n.entry_ids or "[]"):
            e = db.get(Entry, i)
            if e is not None and e.type == "pc" and _halter_weg(db, e):
                offen = True
                break
        if not offen:
            db.delete(n)


def _geaendert(e: Entry) -> None:
    jetzt = utcnow()
    e.updated_at = jetzt
    if e.visibility == "public":
        e.public_changed_at = jetzt


def zu_nsc(db: Session, e: Entry) -> None:
    """POST /entries/{id}/to-npc. Name, Kurzbeschreibung, Status, Sichtbarkeit und Erwähnungen bleiben."""
    if e.type != "pc":
        raise errors.conflict("not_a_character")
    if not _halter_weg(db, e):
        raise errors.conflict("holder_active")
    e.former_holder_member_id = e.holder_member_id
    e.type, e.holder_member_id, e.pc_character_id, e.status = "npc", None, None, None
    _geaendert(e)
    aufraeumen(db, e.campaign_id)


def geben(db: Session, e: Entry, member_id: str) -> None:
    """POST /entries/{id}/assign: Figur einem aktiven Spieler ohne eigene Figur geben. Name und Kurzbeschreibung
    gehen an das Mitglied; die Kennung des Charakters bleibt leer, bis die Person die Figur in ihre Sammlung
    übernimmt (PUT …/members/me/character verknüpft dann diesen Eintrag)."""
    passt = (e.type == "npc" and e.former_holder_member_id is not None) or (e.type == "pc" and _halter_weg(db, e))
    if not passt:
        raise errors.conflict("not_a_character")
    ziel = db.get(Member, member_id)
    if ziel is None or ziel.campaign_id != e.campaign_id or not ziel.aktiv or ziel.role != "player" \
            or ziel.open_seat:
        raise errors.conflict("not_a_player")
    hat = db.scalar(select(Entry.id).where(Entry.campaign_id == e.campaign_id, Entry.type == "pc",
                                           Entry.holder_member_id == ziel.id, Entry.id != e.id).limit(1))
    if hat is not None:
        raise errors.conflict("member_has_character")
    e.type, e.holder_member_id, e.pc_character_id, e.status = "pc", ziel.id, None, None
    e.former_holder_member_id = None
    if ziel.id in e.hidden_member_ids:  # wer die Figur spielt, sieht ihren Eintrag
        e.hidden_from = [h for h in e.hidden_from if h.member_id != ziel.id]
    ziel.character_name = e.name[:200]
    ziel.character_summary = (e.summary or "").strip()[:1000] or None
    _geaendert(e)
    aufraeumen(db, e.campaign_id)

