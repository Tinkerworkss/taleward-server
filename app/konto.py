"""Eigenes Konto: Selbstregistrierung mit Einladungscode, Datenexport, Kontolöschung.

Regeln aus der Schnittstelle (0.3.9):
- Registrierung nur mit gültigem Einladungscode (Einstellung „nur mit Einladung“), sonst 403. Zustimmung zum
  Datenschutzhinweis und Altersbestätigung werden mit Zeitpunkt gespeichert. Anfragen pro Adresse begrenzt (429).
- Export: alles, was der Server über die Person weiß – ohne Stimmabdruck und ohne Inhalte anderer.
- Löschen: Passwort bestätigen. Wer eine Kampagne als einzige SL leitet, in der noch andere sind, muss dort erst
  eine andere SL ernennen (409). Kampagnen, in denen sonst niemand (mehr) ist, werden mitgelöscht.
  Gelöscht werden Konto, Anmeldungen, Charakterbilder, Charakter-Hintergründe, Zustimmungen und Stimmprofil.
  Das Mitglied bleibt als „Gelöschtes Konto“ stehen (Charaktername bleibt Teil der Geschichte), damit Kapitel,
  Anwesende und Kommentare stimmig bleiben. Der Widerruf der Zustimmungen bleibt als Nachweis im Protokoll.
"""
from __future__ import annotations

import re
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import errors, storage
from app.config import get_settings
from app.db import utcnow
from app.models import (
    Attendee, AuthMethod, Campaign, Comment, ConsentLog, DateOption, DatePoll, DateVote, GameSession, Invite, Member,
    Organization, OrgMember, SessionSeen, Upload, User, VoiceProfile,
)
from app.security import hash_password, verify_password
from app.services import normalize_invite_code, set_move_consent, set_recording_consent

REGISTRIERUNG = ("invite_only", "closed")
BENUTZERNAME = re.compile(r"^[\w.-]{3,64}$")
LEICHT = {"passwort", "password", "12345678", "123456789", "1234567890", "qwertzui", "qwertyui", "taleward",
          "admin123", "passwort1", "password1", "11111111", "00000000", "abcdefgh"}

# Begrenzung: höchstens so viele Registrierungsversuche je Adresse im Zeitfenster
MAX_VERSUCHE, FENSTER = 10, 15 * 60


def registrierung(db: Session) -> str:
    from app.einstellungen import meta_lesen

    wert = meta_lesen(db, "registrierung") or get_settings().registration
    return wert if wert in REGISTRIERUNG else "closed"


def _begrenzen(adresse: str) -> None:
    from app.begrenzung import ZAEHLER

    if not ZAEHLER.versuch(f"registrieren:{adresse}", MAX_VERSUCHE, FENSTER):
        raise errors.ApiError(429, "too_many_requests")


def versuche_vergessen() -> None:
    from app.begrenzung import ZAEHLER

    ZAEHLER.vergessen()


def passwort_zu_schwach(passwort: str, *namen: str) -> bool:
    p = passwort.lower()
    return len(passwort) < 8 or p in LEICHT or any(n and p == n.lower() for n in namen) or len(set(p)) < 3


def registrieren(db: Session, adresse: str, invite_code: str, username: str, display_name: str,
                 password: str | None, accept_privacy: bool, age_confirmed: bool,
                 methode: tuple[str, str] | None = None) -> User:
    """Mit Passwort oder (0.4.0) mit einem Anmeldedienst: methode = ("oidc:<dienst>", sub)."""
    _begrenzen(adresse)
    if registrierung(db) != "invite_only":
        raise errors.ApiError(403, "registration_closed")
    inv = db.get(Invite, normalize_invite_code(invite_code or ""))
    if inv is None or inv.expires_at <= utcnow():
        raise errors.ApiError(404, "invite_invalid")
    if accept_privacy is not True or age_confirmed is not True:
        raise errors.bad_request("consent_required")
    name = (username or "").strip().lower()
    if not BENUTZERNAME.fullmatch(name):
        raise errors.bad_request("username_invalid")
    anzeige = (display_name or "").strip()
    if not anzeige:
        raise errors.bad_request("validation_error", "validation_error.empty", field="displayName")
    if len(anzeige) > 128:
        raise errors.bad_request("validation_error", "validation_error.too_long", field="displayName")
    if methode is None and passwort_zu_schwach(password or "", name, anzeige):
        raise errors.bad_request("weak_password")
    if db.scalar(select(User).where(User.username == name)) is not None:
        raise errors.conflict("username_taken")
    jetzt = utcnow()
    u = User(username=name, display_name=anzeige, privacy_accepted_at=jetzt, age_confirmed_at=jetzt)
    db.add(u)
    db.flush()
    if methode is None:
        db.add(AuthMethod(user_id=u.id, kind="password", secret=hash_password(password)))
    else:
        db.add(AuthMethod(user_id=u.id, kind=methode[0], secret=methode[1], last_used_at=jetzt))
    return u


# ---------------------------------------------------------------- Export
def _z(dt) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _charaktere(mitglieder: list[Member], kampagnen: dict) -> list[dict]:
    """Je Charakter aus der Sammlung der App die Kampagnen auf diesem Server (0.4.7)."""
    aus: dict[str, list] = {}
    for m in mitglieder:
        if m.character_id:
            aus.setdefault(m.character_id, []).append({
                "serverCampaignId": m.campaign_id, "title": kampagnen[m.campaign_id].title, "memberId": m.id,
                "joinedAt": _z(m.joined_at), "leftAt": _z(m.left_at)})
    return [{"characterId": k, "campaigns": v} for k, v in aus.items()]


def exportieren(db: Session, u: User, basis_url: str) -> dict:
    from app.einstellungen import angaben

    a = angaben(db)
    mitglieder = list(db.scalars(select(Member).where(Member.user_id == u.id)))
    member_ids = [m.id for m in mitglieder]
    kampagnen = {c.id: c for c in db.scalars(select(Campaign).where(
        Campaign.id.in_([m.campaign_id for m in mitglieder])))} if mitglieder else {}

    def bilder(m: Member):
        if not m.portrait_updated_at:
            return None
        pfad = f"{basis_url}/api/v1/campaigns/{m.campaign_id}/members/{m.id}/portrait"
        return {"full": pfad + "?size=full", "thumb": pfad + "?size=thumb", "updatedAt": _z(m.portrait_updated_at)}

    zustimmungen = db.scalars(select(ConsentLog).where(
        (ConsentLog.user_id == u.id) | (ConsentLog.member_id.in_(member_ids) if member_ids else False))
        .order_by(ConsentLog.at))
    anwesend = db.execute(select(Attendee, GameSession).join(GameSession, GameSession.id == Attendee.session_id)
                          .where(Attendee.member_id.in_(member_ids)).order_by(GameSession.played_at)
                          ).all() if member_ids else []
    vp = db.get(VoiceProfile, u.id)
    orgs = db.execute(select(Organization, OrgMember).join(OrgMember, OrgMember.organization_id == Organization.id)
                      .where(OrgMember.user_id == u.id)).all()
    anmeldungen = db.scalars(select(AuthMethod).where(AuthMethod.user_id == u.id))
    kommentare = [{"id": k.id, "sessionId": k.session_id, "recipientMemberId": k.recipient_member_id,
                   "text": k.text, "createdAt": _z(k.created_at), "editedAt": _z(k.edited_at)}
                  for k in db.scalars(select(Comment).where(Comment.author_member_id.in_(member_ids))
                                      .order_by(Comment.created_at))] if member_ids else []
    stimmen = [{"campaignId": p.campaign_id, "pollId": p.id, "optionStartsAt": _z(o.starts_at), "answer": v.answer,
                "at": _z(v.created_at)}
               for v, o, p in db.execute(select(DateVote, DateOption, DatePoll)
                                         .join(DateOption, DateOption.id == DateVote.option_id)
                                         .join(DatePoll, DatePoll.id == DateOption.poll_id)
                                         .where(DateVote.member_id.in_(member_ids))
                                         .order_by(DateOption.starts_at))] if member_ids else []
    return {
        "format": "taleward-export/1",
        "exportedAt": _z(utcnow()),
        "server": {"name": a.server_name, "operator": a.server_operator, "contact": a.server_contact,
                   "privacyPolicyUrl": a.privacy_policy_url},
        "account": {"id": u.id, "username": u.username, "displayName": u.display_name, "createdAt": _z(u.created_at),
                    "email": u.email, "emailVerifiedAt": _z(u.email_verified_at), "emailPending": u.email_pending,
                    "providers": sorted(m.kind.split(":", 1)[1] for m in u.auth_methods if m.kind.startswith("oidc:")),
                    "privacyAcceptedAt": _z(u.privacy_accepted_at), "ageConfirmedAt": _z(u.age_confirmed_at),
                    "signInMethods": [{"kind": m.kind, "createdAt": _z(m.created_at), "lastUsedAt": _z(m.last_used_at)}
                                      for m in anmeldungen]},
        "organizations": [{"id": o.id, "name": o.name, "role": om.role, "joinedAt": _z(om.joined_at)}
                          for o, om in orgs],
        "memberships": [{
            "campaignId": m.campaign_id, "campaignTitle": kampagnen[m.campaign_id].title, "memberId": m.id,
            "role": m.role, "joinedAt": _z(m.joined_at), "characterName": m.character_name,
            "characterSummary": m.character_summary, "characterBackstory": m.character_backstory,
            "characterId": m.character_id, "characterVersion": m.character_version,
            "characterNickname": m.character_nickname, "characterStatus": m.character_status,
            "recordingConsentAt": _z(m.recording_consent_at), "moveConsentAt": _z(m.move_consent_at),
            "portrait": bilder(m),
        } for m in mitglieder],
        "characters": _charaktere(mitglieder, kampagnen),  # 0.4.7
        "sessionsAttended": [{"campaignId": s.campaign_id, "sessionId": s.id, "number": s.number, "title": s.title,
                              "playedAt": _z(s.played_at), "consent": a_.consent, "consentSource": a_.consent_source,
                              "consentAt": _z(a_.consent_at)} for a_, s in anwesend],
        "consentLog": [{"campaignId": z.campaign_id, "sessionId": z.session_id, "action": z.action, "at": _z(z.at)}
                       for z in zustimmungen],
        "comments": kommentare,
        "dateVotes": stimmen,
        "voiceProfile": None if vp is None else {
            "status": vp.status, "createdAt": _z(vp.created_at), "consentAt": _z(vp.consent_at),
            "sampleSeconds": vp.sample_seconds, "learnFromSessions": vp.learn_from_sessions,
            "learnedSessionCount": vp.learned_session_count,
            "note": "Der Stimmabdruck selbst ist eine Zahlenreihe ohne Aussagekraft außerhalb dieses Servers "
                    "und wird nicht exportiert.",
        },
    }


# ---------------------------------------------------------------- Löschen
def _kampagne_loeschen(db: Session, c: Campaign) -> None:
    """Kampagne, in der sonst niemand ist, samt Dateien löschen."""
    from app.bilder import cover_ordner, loeschen, portrait_ordner

    for s in db.scalars(select(GameSession).where(GameSession.campaign_id == c.id)):
        storage.delete_samples(s.id)
        for up in db.scalars(select(Upload).where(Upload.session_id == s.id)):
            storage.delete_upload_files(up.id)
    for m in c.members:
        loeschen(portrait_ordner(m.id))
    loeschen(cover_ordner(c.id))
    from app import umzug

    umzug.kampagne_entfernt(db, c.id)  # gepackte Umzugsdateien
    from app import kapitelprobe

    kapitelprobe.kampagne_entfernt(c.id)  # Probeläufe der Verwaltung
    from app.models import CampaignDocument
    from app.unterlagen import ordner as unterlagen_ordner

    for doc in db.scalars(select(CampaignDocument).where(CampaignDocument.campaign_id == c.id)):
        shutil.rmtree(unterlagen_ordner(doc.id), ignore_errors=True)
    db.delete(c)


def loeschen(db: Session, u: User, passwort: str | None, bestaetigung: str | None = None) -> None:
    """Bestätigung: Passwort; Konten ohne Passwort (nur Anmeldedienste) mit dem Benutzernamen."""
    methode = db.scalar(select(AuthMethod).where(AuthMethod.user_id == u.id, AuthMethod.kind == "password"))
    if methode is not None or passwort:
        if not verify_password(passwort or "", methode.secret if methode else None):
            raise errors.ApiError(401, "wrong_password")
    elif (bestaetigung or "").strip().lower() != u.username:
        raise errors.bad_request("confirmation_mismatch")
    entfernen(db, u)


def entfernen(db: Session, u: User) -> None:
    """Konto und alles Persönliche daran löschen – ohne Bestätigungsprüfung (die App fragt das Passwort ab, die
    Verwaltung den Benutzernamen). 409 last_gm_campaigns, wenn die Person irgendwo die einzige Spielleitung ist."""
    from app import stimmprofile
    from app.bilder import loeschen as bilder_loeschen, portrait_ordner

    mitglieder = list(db.scalars(select(Member).where(Member.user_id == u.id)))
    blockiert, allein = [], []
    for m in mitglieder:
        if m.role != "gm" or m.left_at is not None:
            continue
        andere = [x for x in m.campaign.members if x.id != m.id and x.aktiv]
        if not andere:
            allein.append(m.campaign)
        elif not any(x.role == "gm" for x in andere):
            blockiert.append(m.campaign.title)
    if blockiert:
        raise errors.conflict("last_gm_campaigns", titel=", ".join(f"„{t}“" for t in sorted(blockiert)))
    for c in allein:
        _kampagne_loeschen(db, c)
    db.flush()
    jetzt = utcnow()
    from app import figuren

    for m in db.scalars(select(Member).where(Member.user_id == u.id)):
        if m.left_at is None:
            figuren.verwaist_melden(db, m)  # 0.4.9: Hinweis an die SL, was aus der Figur wird
        set_recording_consent(db, m, False)  # Widerruf bleibt als Nachweis im Protokoll
        set_move_consent(db, m, False)  # 0.4.8
        bilder_loeschen(portrait_ordner(m.id))
        m.portrait_updated_at = None
        m.character_backstory = None
        m.chronicle_seen_at = m.bible_seen_at = None
        m.user_id, m.deleted_at = None, jetzt
        db.execute(SessionSeen.__table__.delete().where(SessionSeen.member_id == m.id))
        db.execute(DateVote.__table__.delete().where(DateVote.member_id == m.id))  # Kommentare bleiben stehen
    stimmprofile.loeschen(db, u.id)
    from app import umzug

    umzug.konto_entfernt(db, u.id)  # hochgeladene Teile offener Importe
    db.flush()
    db.delete(u)
