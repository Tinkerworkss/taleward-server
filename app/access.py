"""Anmeldung, Rollen und Spoilerschutz – an EINER Stelle.

Grundregel: Was ein Nutzer nicht sehen darf, liefert 404 (die Existenz bleibt verborgen).
403 nur, wenn der Nutzer die Sache ohnehin sehen kann, aber die Aktion der SL vorbehalten ist.
"""
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db, server_id
from app.models import Entry, GameSession, Member, User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise errors.UNAUTHENTICATED
    decoded = decode_token(creds.credentials, server_id())
    if decoded is None:
        raise errors.TOKEN_INVALID
    user_id, version = decoded
    user = db.get(User, user_id)
    if user is None or user.setup_account:
        raise errors.TOKEN_INVALID
    if user.token_version != version:
        # Nach „Passwort ändern“ in der App bleibt nur das Token gültig, mit dem geändert wurde
        from app.security import token_jti

        if not user.token_ausnahme or user.token_ausnahme != f"{version}:{token_jti(creds.credentials)}":
            raise errors.TOKEN_INVALID
    return user


def membership(db: Session, campaign_id: str, user: User) -> Member | None:
    """Aktive Mitgliedschaft – wer die Kampagne verlassen hat, sieht sie nicht mehr (404)."""
    return db.scalar(select(Member).where(Member.campaign_id == campaign_id, Member.user_id == user.id,
                                          Member.left_at.is_(None)))


def aktive_sl_anzahl(db: Session, campaign_id: str) -> int:
    from sqlalchemy import func

    return db.scalar(select(func.count()).select_from(Member).where(
        Member.campaign_id == campaign_id, Member.role == "gm", Member.user_id.is_not(None),
        Member.left_at.is_(None))) or 0


def require_member(db: Session, campaign_id: str, user: User) -> Member:
    m = membership(db, campaign_id, user)
    if m is None:
        raise errors.not_found("campaign")
    return m


def require_gm(member: Member) -> None:
    if member.role != "gm":
        raise errors.forbidden()


def may_see_backstory(viewer: Member, target: Member) -> bool:
    """Charakter-Hintergrund: nur die Person selbst und die SL."""
    return viewer.role == "gm" or viewer.id == target.id


def player_can_see_session(s: GameSession) -> bool:
    return s.state == "published"


@dataclass
class SessionAccess:
    session: GameSession
    member: Member

    @property
    def is_gm(self) -> bool:
        return self.member.role == "gm"


def load_session(db: Session, session_id: str, user: User) -> SessionAccess:
    """Session laden, die der Nutzer sehen darf – sonst 404."""
    s = db.get(GameSession, session_id)
    if s is None:
        raise errors.not_found("session")
    m = membership(db, s.campaign_id, user)
    if m is None:
        raise errors.not_found("session")
    if m.role != "gm" and not player_can_see_session(s):
        raise errors.not_found("session")
    return SessionAccess(s, m)


def load_session_gm(db: Session, session_id: str, user: User) -> SessionAccess:
    """Wie load_session, aber nur für die SL (Spieler bekommen 403 bzw. 404)."""
    acc = load_session(db, session_id, user)
    if not acc.is_gm:
        raise errors.forbidden()
    return acc


@dataclass
class EntryAccess:
    entry: Entry
    member: Member

    @property
    def is_gm(self) -> bool:
        return self.member.role == "gm"


def player_can_see_entry(e: Entry, member: Member) -> bool:
    """Spieler sehen öffentliche Einträge – außer, sie sind gezielt vor ihnen verborgen („Wer weiß was“)."""
    return e.visibility == "public" and member.id not in e.hidden_member_ids


def load_entry(db: Session, entry_id: str, user: User) -> EntryAccess:
    e = db.get(Entry, entry_id)
    if e is None:
        raise errors.not_found("entry")
    m = membership(db, e.campaign_id, user)
    if m is None or (m.role != "gm" and not player_can_see_entry(e, m)):
        raise errors.not_found("entry")
    return EntryAccess(e, m)
