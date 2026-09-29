"""Kommentare zu Kapiteln und Terminabstimmung.

Kommentare:
- Sichtbar ist ein Kapitel wie überall (Spieler nur veröffentlichte, sonst 404).
- Öffentliche Kommentare sehen alle, die das Kapitel sehen. Private nur Absender und Empfänger – auch die SL sieht
  fremde private Nachrichten nicht (bei mehreren SLs).
- Spieler schreiben privat nur an eine SL, die SL an jede Person. Gelöschte Konten sind keine Empfänger.
- Was man nicht sehen darf, gibt es nicht (404). Bearbeiten nur eigene, löschen eigene; die SL alle, die sie sieht.
- Kommentare fließen nie in Recaps oder Vorschläge ein (die Zusammenfassung liest sie nicht).

Terminabstimmung: höchstens eine offene je Kampagne. Alle Mitglieder schlagen vor und stimmen ab; wer vorschlägt,
hat automatisch „yes“ gestimmt. Die SL legt den Termin fest (→ Campaign.nextSessionAt) oder bricht ab.
"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import errors, schemas
from app.access import current_user, load_session, membership, require_gm, require_member
from app.db import get_db, utcnow
from app.models import Campaign, Comment, DateOption, DatePoll, DateVote, GameSession, Member, User

router = APIRouter()
MAX_TEXT = 4000


# ---------------------------------------------------------------- Kommentare
def kommentar_out(k: Comment) -> schemas.CommentOut:
    return schemas.CommentOut(id=k.id, session_id=k.session_id, author_member_id=k.author_member_id,
                              recipient_member_id=k.recipient_member_id, text=k.text, created_at=k.created_at,
                              edited_at=k.edited_at)


def sichtbar_fuer(me: Member):
    """Bedingung: öffentlich oder eigene private Nachricht (geschrieben oder bekommen)."""
    return or_(Comment.recipient_member_id.is_(None), Comment.author_member_id == me.id,
               Comment.recipient_member_id == me.id)


def _text(roh: str) -> str:
    text = (roh or "").strip()
    if not text:
        raise errors.bad_request("comment_empty")
    if len(text) > MAX_TEXT:
        raise errors.ApiError(413, "comment_too_long")
    return text


@router.get("/sessions/{sessionId}/comments", tags=["Kommentare"], response_model=list[schemas.CommentOut])
def comments_list(sessionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    q = (select(Comment).where(Comment.session_id == acc.session.id, sichtbar_fuer(acc.member))
         .order_by(Comment.created_at, Comment.id))
    return [kommentar_out(k) for k in db.scalars(q)]


@router.post("/sessions/{sessionId}/comments", tags=["Kommentare"], status_code=201, response_model=schemas.CommentOut)
def comments_create(sessionId: str, body: schemas.CommentCreate, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    acc = load_session(db, sessionId, user)
    text = _text(body.text)
    empfaenger = None
    if body.recipient_member_id:
        empfaenger = db.get(Member, body.recipient_member_id)
        if (empfaenger is None or empfaenger.campaign_id != acc.session.campaign_id or not empfaenger.aktiv
                or empfaenger.id == acc.member.id):
            raise errors.bad_request("recipient_unknown")
        if not acc.is_gm and empfaenger.role != "gm":
            raise errors.forbidden("forbidden.private_to_gm")
    k = Comment(session_id=acc.session.id, author_member_id=acc.member.id,
                recipient_member_id=empfaenger.id if empfaenger else None, text=text, created_at=utcnow())
    db.add(k)
    db.commit()
    return kommentar_out(k)


def _kommentar(db: Session, comment_id: str, user: User):
    """Kommentar laden, den der Nutzer sehen darf – sonst 404 (auch fremde private Nachrichten)."""
    k = db.get(Comment, comment_id)
    if k is None:
        raise errors.not_found("comment")
    try:
        acc = load_session(db, k.session_id, user)
    except errors.ApiError:
        raise errors.not_found("comment") from None
    me = acc.member
    if k.recipient_member_id is not None and me.id not in (k.author_member_id, k.recipient_member_id):
        raise errors.not_found("comment")
    return k, acc


@router.patch("/comments/{commentId}", tags=["Kommentare"], response_model=schemas.CommentOut)
def comment_edit(commentId: str, body: schemas.CommentPatch, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    k, acc = _kommentar(db, commentId, user)
    if k.author_member_id != acc.member.id:
        raise errors.forbidden("forbidden.own_comment")
    k.text, k.edited_at = _text(body.text), utcnow()
    db.commit()
    return kommentar_out(k)


@router.delete("/comments/{commentId}", tags=["Kommentare"], status_code=204)
def comment_delete(commentId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    k, acc = _kommentar(db, commentId, user)
    if k.author_member_id != acc.member.id and not acc.is_gm:
        raise errors.forbidden("forbidden.delete_comment")
    db.delete(k)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- Ungelesen
def ungelesene_kommentare(db: Session, me: Member, sitzungen: list[GameSession]) -> dict[str, int]:
    """session_id → Anzahl neuer Kommentare anderer, die ich sehen darf, seit meinem Marker des Kapitels
    (fehlt er: Chronik-Marker bzw. Beitritt)."""
    from app.models import SessionSeen

    if not sitzungen:
        return {}
    ids = [s.id for s in sitzungen]
    marker = {row.session_id: row.seen_at for row in db.scalars(
        select(SessionSeen).where(SessionSeen.member_id == me.id, SessionSeen.session_id.in_(ids)))}
    standard = me.chronicle_seen_at or me.joined_at
    out = {i: 0 for i in ids}
    for sid, erstellt in db.execute(select(Comment.session_id, Comment.created_at).where(
            Comment.session_id.in_(ids), Comment.author_member_id != me.id, sichtbar_fuer(me))):
        if erstellt > (marker.get(sid) or standard):
            out[sid] += 1
    return out


def sichtbare_sitzungen(db: Session, me: Member) -> list[GameSession]:
    q = select(GameSession).where(GameSession.campaign_id == me.campaign_id)
    if me.role != "gm":
        q = q.where(GameSession.state == "published")
    return list(db.scalars(q))


# ---------------------------------------------------------------- Terminabstimmung
def umfrage_out(p: DatePoll) -> schemas.DatePollOut:
    return schemas.DatePollOut(
        id=p.id, campaign_id=p.campaign_id, status=p.status, note=p.note, chosen_option_id=p.chosen_option_id,
        created_at=p.created_at,
        options=[schemas.DateOptionOut(id=o.id, starts_at=o.starts_at, proposed_by_member_id=o.proposed_by_member_id,
                                       votes=[schemas.DateVoteOut(member_id=v.member_id, answer=v.answer)
                                              for v in o.votes])
                 for o in sorted(p.options, key=lambda o: o.starts_at)],
    )


def offene_umfrage(db: Session, campaign_id: str) -> DatePoll | None:
    return db.scalar(select(DatePoll).where(DatePoll.campaign_id == campaign_id, DatePoll.status == "open"))


def braucht_meine_stimme(db: Session, me: Member) -> bool:
    p = offene_umfrage(db, me.campaign_id)
    if p is None or not me.aktiv:
        return False
    return any(all(v.member_id != me.id for v in o.votes) for o in p.options)


def _umfrage(db: Session, poll_id: str, user: User) -> tuple[DatePoll, Member]:
    p = db.get(DatePoll, poll_id)
    me = membership(db, p.campaign_id, user) if p else None
    if p is None or me is None:
        raise errors.not_found("date_poll")
    return p, me


def _offen(p: DatePoll) -> None:
    if p.status != "open":
        raise errors.conflict("date_poll_closed")


def _option(p: DatePoll, option_id: str) -> DateOption:
    o = next((o for o in p.options if o.id == option_id), None)
    if o is None:
        raise errors.not_found("option")
    return o


def _stimme(db: Session, o: DateOption, me: Member, antwort: str) -> None:
    v = db.get(DateVote, (o.id, me.id))
    if v is None:
        o.votes.append(DateVote(option_id=o.id, member_id=me.id, answer=antwort, created_at=utcnow()))
    else:
        v.answer = antwort


@router.get("/campaigns/{campaignId}/date-poll", tags=["Termine"], response_model=schemas.DatePollOut)
def poll_get(campaignId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_member(db, campaignId, user)
    p = offene_umfrage(db, campaignId) or db.scalar(
        select(DatePoll).where(DatePoll.campaign_id == campaignId, DatePoll.status == "closed")
        .order_by(DatePoll.ended_at.desc()).limit(1))
    if p is None:
        raise errors.ApiError(404, "no_date_poll")
    return umfrage_out(p)


@router.post("/campaigns/{campaignId}/date-poll", tags=["Termine"], status_code=201, response_model=schemas.DatePollOut)
def poll_create(campaignId: str, body: schemas.DatePollCreate | None = None, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    require_gm(require_member(db, campaignId, user))
    from app.models import Campaign

    if db.get(Campaign, campaignId).archived_at is not None:
        raise errors.conflict("campaign_archived")
    if offene_umfrage(db, campaignId) is not None:
        raise errors.conflict("date_poll_open")
    p = DatePoll(campaign_id=campaignId, status="open", note=((body.note or "").strip() or None) if body else None,
                 created_at=utcnow())
    db.add(p)
    db.commit()
    db.refresh(p)
    return umfrage_out(p)


@router.delete("/date-polls/{pollId}", tags=["Termine"], status_code=204)
def poll_cancel(pollId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p, me = _umfrage(db, pollId, user)
    require_gm(me)
    _offen(p)
    p.status, p.ended_at = "cancelled", utcnow()
    db.commit()
    return Response(status_code=204)


@router.post("/date-polls/{pollId}/options", tags=["Termine"], status_code=201, response_model=schemas.DatePollOut)
def option_create(pollId: str, body: schemas.DateOptionCreate, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    p, me = _umfrage(db, pollId, user)
    _offen(p)
    start = body.starts_at.replace(microsecond=0)
    if start <= utcnow():
        raise errors.bad_request("date_in_past")
    if any(o.starts_at == start for o in p.options):
        raise errors.conflict("option_exists")
    o = DateOption(poll_id=p.id, starts_at=start, proposed_by_member_id=me.id, created_at=utcnow())
    p.options.append(o)
    db.flush()
    _stimme(db, o, me, "yes")
    db.commit()
    db.refresh(p)
    return umfrage_out(p)


@router.delete("/date-polls/{pollId}/options/{optionId}", tags=["Termine"], response_model=schemas.DatePollOut)
def option_delete(pollId: str, optionId: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p, me = _umfrage(db, pollId, user)
    o = _option(p, optionId)
    if o.proposed_by_member_id != me.id and me.role != "gm":
        raise errors.forbidden("forbidden.option")
    _offen(p)
    p.options.remove(o)
    db.commit()
    db.refresh(p)
    return umfrage_out(p)


@router.put("/date-polls/{pollId}/options/{optionId}/vote", tags=["Termine"], response_model=schemas.DatePollOut)
def vote(pollId: str, optionId: str, body: schemas.DateVoteIn, user: User = Depends(current_user),
         db: Session = Depends(get_db)):
    p, me = _umfrage(db, pollId, user)
    o = _option(p, optionId)
    _offen(p)
    _stimme(db, o, me, body.answer)
    db.commit()
    db.refresh(p)
    return umfrage_out(p)


@router.post("/date-polls/{pollId}/close", tags=["Termine"], response_model=schemas.DatePollOut)
def poll_close(pollId: str, body: schemas.DatePollClose, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    p, me = _umfrage(db, pollId, user)
    require_gm(me)
    o = _option(p, body.option_id)
    _offen(p)
    p.status, p.chosen_option_id, p.ended_at = "closed", o.id, utcnow()
    db.get(Campaign, p.campaign_id).next_session_at = o.starts_at
    db.commit()
    db.refresh(p)
    return umfrage_out(p)
