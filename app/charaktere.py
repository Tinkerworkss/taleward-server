"""Charaktere (Schnittstelle 0.4.7).

Die App ist Ort der Wahrheit: Sie vergibt die Kennung eines Charakters und zählt seine Fassung hoch. Der Server hält
am Mitglied eine Kopie (Member.character_*) und bearbeitet sie nie selbst. Dazu gehört ein öffentlicher Bibel-Eintrag
der Art pc, den der Server mit Name und Kurzbeschreibung synchron hält.

Mitgebrachte Welt (Orte, NSCs … aus der Sammlung der App) wird nie direkt zu Einträgen, sondern zu Vorschlägen mit
source character, über die die SL sofort entscheidet. Geheimes ist öffentlich, aber vor allen anderen Spielern
verborgen (hiddenFromMemberIds).

Spoilerschutz wie überall: Spieler sehen keine Vorschläge – auch nicht die eigenen, nur deren Stand.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors, schemas
from app.db import utcnow
from app.models import Campaign, Entry, EntryHidden, GmNotice, Member, Proposal

MAX_WELT = 100
CHARAKTER_PROPOSALS_ENTSCHIEDEN = 50


# ---------------------------------------------------------------- Charakter am Mitglied
def _anderer_mit(db: Session, me: Member, character_id: str) -> bool:
    return db.scalar(select(Member.id).where(
        Member.campaign_id == me.campaign_id, Member.character_id == character_id, Member.id != me.id,
        Member.user_id.is_not(None), Member.left_at.is_(None)).limit(1)) is not None


def _pc_eintrag(db: Session, me: Member) -> Entry | None:
    """pc-Eintrag des aktuellen Charakters: über das Mitglied, sonst über die Kennung (nach Lösen und Wiederkehr)."""
    e = db.scalar(select(Entry).where(Entry.campaign_id == me.campaign_id, Entry.type == "pc",
                                      Entry.holder_member_id == me.id).limit(1))
    if e is None and me.character_id:
        e = db.scalar(select(Entry).where(Entry.campaign_id == me.campaign_id, Entry.type == "pc",
                                          Entry.pc_character_id == me.character_id).limit(1))
    return e


def _pc_abgleichen(db: Session, me: Member) -> None:
    """pc-Eintrag anlegen bzw. Name und Kurzbeschreibung übernehmen."""
    jetzt = utcnow()
    e = _pc_eintrag(db, me)
    if e is None:
        e = Entry(campaign_id=me.campaign_id, type="pc", name=me.character_name or "?", summary="",
                  visibility="public", created_at=jetzt)
        db.add(e)
    alt = (e.name, e.summary, e.holder_member_id)
    e.name = (me.character_name or e.name)[:300]
    e.summary = me.character_summary or ""
    e.holder_member_id = me.id
    e.pc_character_id = me.character_id
    if (e.name, e.summary, e.holder_member_id) != alt or e.public_changed_at is None:
        e.updated_at = jetzt
        if e.visibility == "public":
            e.public_changed_at = jetzt


def _uebernehmen(me: Member, ch: schemas.CharacterIn) -> None:
    me.character_id = ch.id.lower()
    me.character_version = ch.version
    me.character_status = ch.status
    me.character_name = ch.name.strip()[:200] or me.character_name
    me.character_nickname = (ch.nickname or "").strip() or None
    me.character_summary = (ch.summary or "").strip() or None
    me.character_backstory = (ch.backstory or "").strip() or None
    me.character_system = (ch.system or "").strip() or None


def setzen(db: Session, me: Member, ch: schemas.CharacterIn, beitritt: bool = False) -> None:
    """PUT …/members/me/character bzw. Beitritt mit Charakter.

    Ohne bisherigen Charakter: übernehmen. Sonst nur dieselbe Kennung (409 character_mismatch) und nur eine höhere
    Fassung (409 character_version_stale mit details.serverVersion) – beim Beitritt wird eine gleiche oder ältere
    Fassung still übergangen (die App schickt beim Wiederbeitritt einfach ihren Stand)."""
    kennung = ch.id.lower()
    if me.character_id is None:
        if _anderer_mit(db, me, kennung):
            raise errors.conflict("character_in_campaign")
    elif me.character_id != kennung:
        raise errors.conflict("character_mismatch")
    elif ch.version <= (me.character_version or 0):
        if beitritt:
            return
        raise errors.ApiError(409, "character_version_stale", details={"serverVersion": me.character_version})
    _uebernehmen(me, ch)
    db.flush()
    _pc_abgleichen(db, me)


def loesen(db: Session, me: Member) -> None:
    """DELETE …/members/me/character: Kennung lösen, der pc-Eintrag bleibt ohne Halter stehen."""
    if me.character_id is None:
        raise errors.conflict("no_character")
    e = _pc_eintrag(db, me)
    if e is not None:
        e.holder_member_id = None
        e.updated_at = utcnow()
    me.character_id = me.character_version = me.character_status = None
    me.character_nickname = me.character_system = None


# ---------------------------------------------------------------- Neuzugang
def neuzugang(db: Session, me: Member) -> None:
    """Regel 0.4.7: Wer neu dazukommt, wird in alle nicht-leeren Verborgen-Listen aufgenommen (Einträge und offene
    Vorschläge) – wer etwas nicht miterlebt hat, soll es nicht plötzlich wissen. Die SL bekommt einen Hinweis und
    gibt einzeln frei."""
    betroffen = []
    for e in db.scalars(select(Entry).where(Entry.campaign_id == me.campaign_id)):
        ids = e.hidden_member_ids
        if ids and me.id not in ids:
            e.hidden_from.append(EntryHidden(entry_id=e.id, member_id=me.id))
            betroffen.append(e.id)
    for p in db.scalars(select(Proposal).where(Proposal.campaign_id == me.campaign_id, Proposal.decision == "open")):
        ids = json.loads(p.hidden_member_ids_json or "[]")
        if ids and me.id not in ids:
            p.hidden_member_ids_json = json.dumps(sorted(set(ids) | {me.id}))
    if betroffen:
        db.add(GmNotice(campaign_id=me.campaign_id, code="hidden_entries_for_newcomer", member_id=me.id,
                        entry_ids=json.dumps(sorted(betroffen))))


def beitritt_melden(db: Session, me: Member) -> None:
    """0.4.14 (member_joined): Die SL erfährt von jedem Beitritt, auch vom Wiedereintritt und vom Beitritt mit
    Platz-Code, und kann den Hinweis erledigen oder die Person entfernen."""
    beitritt_vergessen(db, me.id)
    db.add(GmNotice(campaign_id=me.campaign_id, code="member_joined", member_id=me.id, entry_ids="[]"))


def beitritt_vergessen(db: Session, member_id: str) -> None:
    db.execute(GmNotice.__table__.delete().where(GmNotice.member_id == member_id, GmNotice.code == "member_joined"))


def hinweise(db: Session, c: Campaign) -> list[schemas.GmNoticeOut]:
    vorhanden = set(db.scalars(select(Entry.id).where(Entry.campaign_id == c.id)))
    return [schemas.GmNoticeOut(id=n.id, code=n.code, member_id=n.member_id, created_at=n.created_at,
                                entry_ids=[i for i in json.loads(n.entry_ids or "[]") if i in vorhanden])
            for n in db.scalars(select(GmNotice).where(GmNotice.campaign_id == c.id).order_by(GmNotice.created_at))]


# ---------------------------------------------------------------- Mitgebrachte Welt
def _herkunft(stmt, character_id: str, origin_entry_id: str):
    return stmt.where(Proposal.source == "character", Proposal.origin_character_id == character_id,
                      Proposal.origin_entry_id == origin_entry_id)


def _eintrag_zu(db: Session, campaign_id: str, character_id: str, origin_entry_id: str) -> Entry | None:
    return db.scalar(select(Entry).where(Entry.campaign_id == campaign_id, Entry.origin_character_id == character_id,
                                         Entry.origin_entry_id == origin_entry_id).limit(1))


def _stand(p: Proposal | None, e: Entry | None, kennung: str, zustand: str | None = None) -> schemas.WorldEntryStatusOut:
    if zustand is None:
        zustand = {"open": "pending", "accepted": "accepted", "rejected": "rejected"}[p.decision] if p else "unchanged"
    return schemas.WorldEntryStatusOut(id=kennung, proposal_id=p.id if p else None, entry_id=e.id if e else None,
                                       state=zustand, server_version=e.origin_version if e else None)


def einreichen(db: Session, me: Member, eintraege: list[schemas.WorldEntryIn]) -> list[schemas.WorldEntryStatusOut]:
    if me.character_id is None:
        raise errors.conflict("no_character")
    if not 1 <= len(eintraege) <= MAX_WELT:
        raise errors.bad_request("world_too_many")
    andere = sorted(m.id for m in db.scalars(select(Member).where(Member.campaign_id == me.campaign_id))
                    if m.aktiv and m.id != me.id and m.role != "gm")
    aus = []
    for pos, w in enumerate(eintraege):
        kennung = w.id.lower()
        e = _eintrag_zu(db, me.campaign_id, me.character_id, kennung)
        if e is not None and w.version <= (e.origin_version or 0):
            aus.append(_stand(None, e, kennung, "unchanged"))
            continue
        offen = list(db.scalars(_herkunft(select(Proposal), me.character_id, kennung)
                                .where(Proposal.decision == "open")))
        abgelehnt = db.scalar(_herkunft(select(Proposal), me.character_id, kennung)
                              .where(Proposal.decision == "rejected", Proposal.origin_version >= w.version)
                              .order_by(Proposal.decided_at.desc()).limit(1))
        if abgelehnt is not None and not offen:
            aus.append(_stand(abgelehnt, e, kennung))  # diese Fassung hat die SL schon abgelehnt
            continue
        for alt in offen:  # offener Vorschlag zu derselben Kennung wird ersetzt
            db.delete(alt)
        p = Proposal(
            campaign_id=me.campaign_id, source="character", origin_character_id=me.character_id,
            origin_entry_id=kennung, origin_version=w.version, submitted_by_member_id=me.id, position=pos,
            entry_type=w.type, action="update" if e is not None else "create",
            target_entry_id=e.id if e is not None else None, title=w.name.strip()[:300], detail=w.summary.strip(),
            suggested_visibility="public", hidden_member_ids_json=json.dumps(andere if w.secret else []),
            confidence=1.0, flags="[]", evidence="[]", decision="open",
        )
        db.add(p)
        db.flush()
        aus.append(_stand(p, e, kennung))
    return aus


def stand(db: Session, me: Member) -> list[schemas.WorldEntryStatusOut]:
    """Stand aller je eingereichten Einträge dieses Mitglieds (jeweils der neueste Vorschlag)."""
    neueste: dict[tuple[str, str], Proposal] = {}
    for p in db.scalars(select(Proposal).where(Proposal.source == "character", Proposal.submitted_by_member_id == me.id)
                        .order_by(Proposal.created_at)):
        neueste[(p.origin_character_id or "", p.origin_entry_id or "")] = p
    return [_stand(p, _eintrag_zu(db, me.campaign_id, cid, oid), oid) for (cid, oid), p in neueste.items()]


def offene_anzahl(db: Session, campaign_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.campaign_id == campaign_id, Proposal.source == "character", Proposal.decision == "open")) or 0


def vorschlaege(db: Session, campaign_id: str) -> list[Proposal]:
    """Prüfliste der SL: offene zuerst, danach die letzten 50 entschiedenen."""
    basis = select(Proposal).where(Proposal.campaign_id == campaign_id, Proposal.source == "character")
    offen = list(db.scalars(basis.where(Proposal.decision == "open").order_by(Proposal.created_at)))
    fertig = list(db.scalars(basis.where(Proposal.decision != "open").order_by(Proposal.decided_at.desc())
                             .limit(CHARAKTER_PROPOSALS_ENTSCHIEDEN)))
    return offen + fertig


def entscheiden(db: Session, p: Proposal, mitglieder: set[str]) -> None:
    """Bei source character wirkt accepted/rejected sofort (kein Veröffentlichen, kein apply)."""
    from app.services import apply_entry_input

    p.decided_at = utcnow()
    if p.decision != "accepted":
        return
    oeffentlich = p.suggested_visibility == "public"
    verborgen = [m for m in json.loads(p.hidden_member_ids_json or "[]") if m in mitglieder] if oeffentlich else []
    e = db.get(Entry, p.target_entry_id or "") if p.action == "update" else None
    if e is None or e.campaign_id != p.campaign_id:
        e = Entry(campaign_id=p.campaign_id, type=p.entry_type, name=p.title, visibility="gm_only")
        db.add(e)
        db.flush()
        daten = dict(type=p.entry_type, name=p.title, summary=p.detail, gm_notes=p.gm_notes or "",
                     visibility=p.suggested_visibility, hidden_from_member_ids=verborgen)
    else:
        daten = dict(name=p.title, summary=p.detail)
        if p.gm_notes:
            daten["gm_notes"] = "\n\n".join(t for t in ((e.gm_notes or "").strip(), p.gm_notes.strip()) if t)
        if verborgen and e.visibility == "public":
            daten["hidden_from_member_ids"] = sorted(set(e.hidden_member_ids) | set(verborgen))
    apply_entry_input(db, e, schemas.EntryInput(**daten))
    e.origin_character_id, e.origin_entry_id = p.origin_character_id, p.origin_entry_id
    e.origin_version, e.origin_member_id = p.origin_version, p.submitted_by_member_id
    p.target_entry_id = e.id


# ---------------------------------------------------------------- Chronik
def _z(dt) -> str | None:
    if dt is None:
        return None
    from app.services import _utc

    return _utc(dt).isoformat().replace("+00:00", "Z")


def chronik(db: Session, me: Member, request) -> dict:
    """Abschrift aus Sicht der Urheberin. Nie: Transkripte, Hörproben, SL-Notizen, gm_only, Vorschläge, Namen
    anderer Personen."""
    from app.einstellungen import angaben, oeffentliche_adresse
    from app.models import Attendee, Comment, GameSession, Recap
    from app.routers.auth import API_VERSION

    c = db.get(Campaign, me.campaign_id)
    bis = me.left_at
    sitzungen = list(db.scalars(select(GameSession).where(GameSession.campaign_id == c.id,
                                                          GameSession.state == "published")
                                .order_by(GameSession.number)))
    if bis is not None:
        sitzungen = [s for s in sitzungen if s.published_at is None or s.published_at <= bis]
    da = set(db.scalars(select(Attendee.session_id).where(Attendee.member_id == me.id)))
    kapitel = []
    for s in sitzungen:
        recap = None
        r = db.get(Recap, s.id) if s.id in da else None
        if r is not None:
            recap = {"title": r.title, "text": r.text, "openThreads": json.loads(r.open_threads or "[]"),
                     "publishedAt": _z(s.published_at)}
        kapitel.append({"id": s.id, "number": s.number, "title": s.title, "playedAt": _z(s.played_at),
                        "attended": s.id in da, "recap": recap})
    namen = [n.casefold() for n in (me.character_name, me.character_nickname) if n and len(n) >= 2]
    erwaehnt, mitgebracht = [], []
    for e in db.scalars(select(Entry).where(Entry.campaign_id == c.id, Entry.visibility == "public")
                        .order_by(Entry.name)):
        if me.id in e.hidden_member_ids and me.role != "gm":
            continue
        if bis is not None:
            # Ehemalige: nur, was bis zum Austritt so stand. Später Angelegtes oder Geändertes und alles, was die SL
            # vor einzelnen verborgen hat (Ehemalige stehen in keiner Verborgen-Liste), bleibt draußen.
            if e.updated_at is None or e.updated_at > bis:
                continue
            if e.hidden_member_ids and e.origin_member_id != me.id:
                continue
        if e.origin_member_id == me.id:
            mitgebracht.append({"originEntryId": e.origin_entry_id, "entryId": e.id, "entryType": e.type,
                                "name": e.name, "summary": e.summary, "hidden": bool(e.hidden_member_ids),
                                "updatedAt": _z(e.updated_at)})
        text = f"{e.name}\n{e.summary}".casefold()
        if namen and any(n in text for n in namen):
            erwaehnt.append({"entryId": e.id, "entryType": e.type, "name": e.name, "summary": e.summary,
                             "updatedAt": _z(e.updated_at)})
    kommentare = [{"sessionId": k.session_id, "text": k.text, "createdAt": _z(k.created_at)}
                  for k in db.scalars(select(Comment).where(Comment.author_member_id == me.id)
                                      .order_by(Comment.created_at))]
    return {
        "server": {"name": angaben(db).server_name, "url": oeffentliche_adresse(db, request), "version": API_VERSION},
        "campaign": {"id": c.id, "title": c.title, "system": c.system_name or c.system, "language": c.language,
                     "createdAt": _z(c.created_at), "archivedAt": _z(c.archived_at)},
        "member": {"id": me.id, "role": me.role, "joinedAt": _z(me.joined_at), "leftAt": _z(me.left_at),
                   "characterId": me.character_id, "characterVersion": me.character_version},
        "sessions": kapitel, "mentions": erwaehnt, "broughtEntries": mitgebracht, "comments": kommentare,
        "takenAt": _z(utcnow()),
    }
