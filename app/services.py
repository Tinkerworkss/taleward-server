"""Umwandlung DB → API-Antworten und Fachlogik, die mehrere Router brauchen."""
import random
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import errors, links, schemas
from app.access import may_see_backstory
from app.config import get_settings
from app.db import utcnow
from app.models import (
    Attendee, Campaign, ConsentLog, Entry, EntryHidden, GameSession, Invite, Member, Organization, OrgMember, User,
)

# Zustände, in denen die SL etwas tun muss
GM_ACTION_STATES = ("awaiting_speakers", "awaiting_review")


def _utc(dt: datetime | None) -> datetime | None:
    """Aggregat-Abfragen umgehen den Spaltentyp und liefern naive Zeiten."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- Konten & Organisation ----------
def cloud_anbieter(k) -> str:
    """Kennung des Cloud-Anbieters für ServerInfo.cloudSummary: bekannte Anbieter mit ihrer Kennung
    (app/cloudanbieter.py), sonst der Host der eingestellten Adresse."""
    from urllib.parse import urlsplit

    a = k.anbieter
    return a.id if a else (urlsplit(k.api_url).hostname or "api")


def cloud_anbieter_name(k) -> str:
    """Lesbarer Name für Hinweise an Nutzer („… über Mistral (EU)“)."""
    a = k.anbieter
    return a.name if a else cloud_anbieter(k)


def user_out(u: User) -> schemas.UserOut:
    """Eigenes Profil (nur für die Person selbst)."""
    arten = [a.kind for a in u.auth_methods]
    return schemas.UserOut(id=u.id, username=u.username, display_name=u.display_name, email=u.email,
                           email_pending=u.email_pending, has_password="password" in arten,
                           providers=sorted(k.split(":", 1)[1] for k in arten if k.startswith("oidc:")))


def default_organization(db: Session) -> Organization:
    org = db.scalar(select(Organization).order_by(Organization.created_at))
    if org is None:
        org = Organization(name=get_settings().organization_name)
        db.add(org)
        db.flush()
    return org


def ensure_org_member(db: Session, org_id: str, user: User, role: str = "member") -> None:
    if db.get(OrgMember, (org_id, user.id)) is None:
        db.add(OrgMember(organization_id=org_id, user_id=user.id, role=role))
        db.flush()


def user_org_ids(db: Session, user: User) -> list[str]:
    return list(db.scalars(select(OrgMember.organization_id).where(OrgMember.user_id == user.id)))


def choose_organization(db: Session, user: User, requested: str | None) -> str:
    """Organisation für eine neue Kampagne bestimmen."""
    mine = user_org_ids(db, user)
    if requested:
        if requested not in mine:
            raise errors.bad_request("organization_invalid")
        return requested
    if len(mine) == 1:
        return mine[0]
    if not mine:  # Konto aus der Zeit vor Organisationen oder von Hand angelegt
        org = default_organization(db)
        ensure_org_member(db, org.id, user)
        return org.id
    raise errors.bad_request("organization_required")


# ---------- Mitglieder ----------
def member_out(m: Member, viewer: Member | None) -> schemas.MemberOut:
    daten = dict(
        id=m.id, user_id=m.user_id or "", display_name=m.anzeigename, character_name=m.character_name,
        role=m.role, recording_consent_at=m.recording_consent_at, character_summary=m.character_summary,
        portrait_updated_at=m.portrait_updated_at, deleted_at=m.deleted_at, left_at=m.left_at,
        character_status=m.character_status, character_nickname=m.character_nickname,
        move_consent_at=m.move_consent_at, open_seat=m.open_seat,
    )
    if viewer is not None and may_see_backstory(viewer, m):
        daten["character_backstory"] = m.character_backstory
        # 0.4.8: die Kennung ist der Schlüssel zu einem offenen Platz – nur die Person selbst und die SL
        daten.update(character_id=m.character_id, character_version=m.character_version)
    return schemas.MemberOut(**daten)


def member_label(m: Member) -> str:
    return m.anzeigename + (f" ({m.character_name})" if m.character_name else "")


def set_move_consent(db: Session, me: Member, granted: bool) -> None:
    """0.4.8: Zustimmung, dass die eigenen Charakterdaten bei einem Umzug mitgehen – protokolliert wie die
    Aufnahme-Einwilligung (move_granted / move_revoked)."""
    now = utcnow()
    if granted:
        if me.move_consent_at is not None:
            return
        me.move_consent_at = now
        action = "move_granted"
    else:
        if me.move_consent_at is None:
            return
        me.move_consent_at = None
        action = "move_revoked"
    db.add(ConsentLog(campaign_id=me.campaign_id, member_id=me.id, user_id=me.user_id, action=action,
                      recorded_by_user_id=me.user_id, at=now))


def set_recording_consent(db: Session, me: Member, granted: bool) -> None:
    """Stehende Zustimmung setzen oder widerrufen – jede Änderung wird protokolliert."""
    now = utcnow()
    if granted:
        me.recording_consent_at = now
        action = "granted"
    else:
        if me.recording_consent_at is None:
            return
        me.recording_consent_at = None
        action = "revoked"
    db.add(ConsentLog(campaign_id=me.campaign_id, member_id=me.id, user_id=me.user_id, action=action,
                      recorded_by_user_id=me.user_id, at=now))


# ---------- Kampagnen ----------
def _unread(db: Session, c: Campaign, me: Member) -> schemas.UnreadOut:
    from app.routers.miteinander import sichtbare_sitzungen, ungelesene_kommentare

    kommentare = sum(ungelesene_kommentare(db, me, sichtbare_sitzungen(db, me)).values())
    if me.role == "gm":  # Recaps veröffentlicht die SL selbst, die Bibel pflegt sie selbst
        return schemas.UnreadOut(recaps=0, comments=kommentare, bible=0)
    chronik = me.chronicle_seen_at or me.joined_at
    bibel = me.bible_seen_at or me.joined_at
    recaps = db.scalar(
        select(func.count()).select_from(GameSession).where(
            GameSession.campaign_id == c.id, GameSession.state == "published", GameSession.published_at > chronik
        )
    )
    verborgen = select(EntryHidden.entry_id).where(EntryHidden.member_id == me.id)
    bible = db.scalar(
        select(func.count()).select_from(Entry).where(
            Entry.campaign_id == c.id, Entry.visibility == "public", Entry.public_changed_at > bibel,
            Entry.id.not_in(verborgen),
        )
    )
    return schemas.UnreadOut(recaps=recaps, comments=kommentare, bible=bible)


def _summary_fields(db: Session, c: Campaign, me: Member) -> dict:
    from app.routers.miteinander import braucht_meine_stimme

    member_count = db.scalar(select(func.count()).select_from(Member).where(
        Member.campaign_id == c.id, Member.user_id.is_not(None), Member.left_at.is_(None)))
    published = db.execute(
        select(func.count(), func.max(GameSession.published_at)).where(
            GameSession.campaign_id == c.id, GameSession.state == "published"
        )
    ).one()
    pending = mitgebracht = 0
    if me.role == "gm":
        from app.charaktere import offene_anzahl

        mitgebracht = offene_anzahl(db, c.id)
        pending = db.scalar(
            select(func.count()).select_from(GameSession).where(
                GameSession.campaign_id == c.id, GameSession.state.in_(GM_ACTION_STATES)
            )
        )
    return dict(
        id=c.id, title=c.title,
        organization=schemas.OrgRef(id=c.organization.id, name=c.organization.name) if c.organization else None,
        my_role=me.role, my_character_name=me.character_name,
        member_count=member_count, archived_at=c.archived_at, published_session_count=published[0], pending_review_count=pending,
        open_character_proposals=mitgebracht,
        last_published_at=_utc(published[1]), cover_preset=c.cover_preset, unread=_unread(db, c, me),
        cover_image_updated_at=c.cover_image_updated_at, next_session_at=c.next_session_at,
        date_poll_needs_my_vote=braucht_meine_stimme(db, me),
    )


def campaign_summary(db: Session, c: Campaign, me: Member) -> schemas.CampaignSummaryOut:
    return schemas.CampaignSummaryOut(**_summary_fields(db, c, me))


def campaign_out(db: Session, c: Campaign, me: Member) -> schemas.CampaignOut:
    members = sorted(c.members, key=lambda m: (m.role != "gm", m.joined_at))
    out = schemas.CampaignOut(
        **_summary_fields(db, c, me), description=c.description, language=c.language, system=c.system,
        system_name=c.system_name, world_info=c.world_info,
        allow_external_transcription=c.allow_external_transcription, allow_cloud_summary=c.allow_cloud_summary,
        members=[member_out(m, me) for m in members],
        links=links.lesen(c.links, nur_geteilt=me.role != "gm"),  # 0.4.13
    )
    if me.role == "gm":  # Namenshilfe (0.4.6) nur für die SL – für Spieler gar nicht im JSON
        from app import namenshilfe

        out.hotwords = namenshilfe.anzeige(db, c)
        from app.charaktere import hinweise

        out.gm_notices = hinweise(db, c)  # 0.4.7
    return out


def random_cover() -> str:
    return random.choice(schemas.COVER_PRESETS)


# ---------- Einladungen ----------
_WORDS = [
    "RABE", "WOLF", "EICHE", "DRACHE", "FUCHS", "BURG", "KRONE", "FACKEL", "KRUG", "SCHILD",
    "EULE", "HIRSCH", "TURM", "RUNE", "SCHWERT", "ANKER", "LATERNE", "KESSEL", "GREIF", "NEBEL",
    "MOND", "FELS", "BOGEN", "HORN", "MANTEL", "PFAD", "QUELLE", "SPINNE", "TROLL", "ZAUBER",
]


def new_invite_code(db: Session) -> str:
    for _ in range(50):
        # Wort + 8 Ziffern (≈ 31 Bit); passt zum bisherigen Muster „WORT-Ziffern“ der App
        code = f"{secrets.choice(_WORDS)}-{secrets.randbelow(10**8):08d}"
        if db.get(Invite, code) is None:
            return code
    raise RuntimeError("Kein freier Einladungscode gefunden")


def create_invite(db: Session, campaign_id: str, user: User) -> Invite:
    inv = Invite(
        code=new_invite_code(db), campaign_id=campaign_id, created_by=user.id,
        expires_at=(utcnow() + timedelta(days=get_settings().invite_ttl_days)).replace(microsecond=0),
    )
    db.add(inv)
    return inv


def normalize_invite_code(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


# ---------- Sessions ----------
def session_summary_out(s: GameSession, ungelesen: int = 0) -> schemas.SessionSummaryOut:
    return schemas.SessionSummaryOut(id=s.id, number=s.number, title=s.title, played_at=s.played_at,
                                     state=s.state, unread_comments=ungelesen)


def attendee_out(a: Attendee) -> schemas.AttendeeOut:
    daten = dict(consent=a.consent, consent_source=a.consent_source, consent_at=a.consent_at)
    if a.member_id:
        daten["member_id"] = a.member_id
    else:
        daten["guest_name"] = a.guest_name
    return schemas.AttendeeOut(**daten)


def session_out(s: GameSession, db: Session | None = None, me: Member | None = None) -> schemas.SessionOut:
    ungelesen = 0
    if db is not None and me is not None:
        from app.routers.miteinander import ungelesene_kommentare

        ungelesen = ungelesene_kommentare(db, me, [s]).get(s.id, 0)
    return schemas.SessionOut(
        id=s.id, number=s.number, title=s.title, played_at=s.played_at, state=s.state, unread_comments=ungelesen,
        campaign_id=s.campaign_id, source=s.source, transcription_engine=s.transcription_engine, attendees=[attendee_out(a) for a in s.attendees],
        duration_seconds=s.duration_seconds, audio_deleted_at=s.audio_deleted_at, published_at=s.published_at,
    )


def render_status_message(raw: str | None, lang: str) -> str | None:
    """status_message ist ein JSON-Schlüssel (siehe queue.status_message) oder alter Freitext."""
    if not raw:
        return None
    import json

    try:
        daten = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(daten, dict) or "key" not in daten:
        return raw
    key = daten.pop("key")
    return errors.ApiError(0, key, key, **daten).message(lang)


def processing_status(db: Session, s: GameSession, lang: str = "de") -> schemas.ProcessingStatusOut:
    from app import queue  # vermeidet Importschleife

    position = None
    message = render_status_message(s.status_message, lang)
    if s.state == "queued":
        job = queue.current_job(db, s.id)
        if job is not None:
            position = queue.queue_position(db, job)
        if not queue.worker_online(db, "asr"):
            message = errors.ApiError(0, "status.worker_paused" if queue.pausierte_worker(db, "asr")
                                      else "status.no_worker").message(lang)
            from app import extern

            name = extern.anbieter(db)
            from app import kosten

            if name and kosten.erreicht(db):
                message = errors.ApiError(0, "status.cost_limit").message(lang)
            elif job is not None and name and extern.erlaubt(db, job):
                zeit = extern.ab_wann(db, job).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin"))
                message = errors.ApiError(0, "status.external_planned", anbieter=extern.ANBIETER[name],
                                          zeit=zeit.strftime("%d.%m. %H:%M")).message(lang)
    elif s.state == "summarizing" and (s.status_message or "").startswith("summarizing."):
        pass  # es arbeitet schon jemand daran – Zwischenschritt für die App (0.4.6)
    elif s.state == "summarizing":
        from app.einstellungen import llm_konfig

        from app import kosten

        k = llm_konfig(db)
        if k.art == "lokal":
            if not queue.llm_worker_online(db):
                message = errors.ApiError(0, "status.no_llm_worker").message(lang)
        elif k.art == "api" and not db.get(Campaign, s.campaign_id).allow_cloud_summary:
            message = errors.ApiError(0, "status.cloud_summary_not_allowed",
                                      anbieter=cloud_anbieter_name(k)).message(lang)
        elif k.art == "api" and kosten.erreicht(db):
            message = errors.ApiError(0, "status.cost_limit").message(lang)
        elif not k.bereit:
            message = errors.ApiError(0, "status.no_summarizer").message(lang)
    return schemas.ProcessingStatusOut(
        state=s.state, progress=s.progress, queue_position=position,
        message=message, updated_at=s.state_updated_at, estimated_seconds=restdauer(db, s),
    )


def restdauer(db: Session, s: GameSession) -> int | None:
    """Geschätzte Restdauer einer erneuten Transkription samt Zusammenfassung (0.4.6) – aus der letzten Rechenzeit
    dieser Session. Sonst None."""
    if not s.nachtranskription or s.state not in ("queued", "transcribing"):
        return None
    from app.models import UsageLog

    summe = 0.0
    for art in ("transcription", "summary"):
        sekunden = db.scalar(select(UsageLog.compute_seconds).where(UsageLog.session_id == s.id, UsageLog.kind == art)
                             .order_by(UsageLog.created_at.desc()).limit(1))
        summe += float(sekunden or 0)
    if summe <= 0:
        return None
    return int(round(summe * (1 - (s.progress or 0) * 0.7 if s.state == "transcribing" else 1)))


def build_attendees(db: Session, campaign_id: str, items: list[schemas.AttendeeIn]) -> list[Attendee]:
    """Prüft die Anwesenden nach den Einwilligungsregeln der Schnittstelle.

    - genau eines von memberId oder guestName
    - Gäste nur mit consentSource on_site
    - consentSource app nur mit stehender Zustimmung des Mitglieds (sonst 409 consent_missing)
    - Keine stellvertretende Zustimmung: „app“ übernimmt ausschließlich die eigene Zustimmung des Mitglieds.
    """
    if not items:
        raise errors.bad_request("attendees_empty")
    members = {m.id: m for m in db.scalars(select(Member).where(Member.campaign_id == campaign_id))}
    seen: set[str] = set()
    out = []
    now = utcnow()
    for pos, a in enumerate(items):
        guest = (a.guest_name or "").strip()
        if bool(a.member_id) == bool(guest):
            raise errors.bad_request("invalid_attendee")
        if guest:
            if a.consent_source != "on_site":
                raise errors.bad_request("invalid_attendee", "invalid_attendee.guest_app")
            out.append(Attendee(guest_name=guest, position=pos, consent=a.consent, consent_source="on_site",
                                consent_at=(a.consent_at or now) if a.consent else None))
            continue
        if a.member_id in seen:
            raise errors.bad_request("attendee_duplicate")
        m = members.get(a.member_id)
        if m is None or not m.aktiv:  # gelöschte Konten können nicht mehr teilnehmen
            raise errors.bad_request("attendee_unknown")
        seen.add(a.member_id)
        if a.consent_source == "app":
            if a.consent and m.recording_consent_at is None:
                raise errors.conflict("consent_missing", name=member_label(m))
            out.append(Attendee(member_id=m.id, position=pos, consent=a.consent, consent_source="app",
                                consent_at=m.recording_consent_at if a.consent else None))
        else:
            out.append(Attendee(member_id=m.id, position=pos, consent=a.consent, consent_source="on_site",
                                consent_at=(a.consent_at or now) if a.consent else None))
    return out


def log_on_site_consents(db: Session, s: GameSession, recorded_by: User) -> None:
    """Vor Ort gegebene Zustimmungen protokollieren (Nachweis)."""
    members = {m.id: m for m in db.scalars(select(Member).where(Member.campaign_id == s.campaign_id))}
    for a in s.attendees:
        if a.consent and a.consent_source == "on_site":
            m = members.get(a.member_id) if a.member_id else None
            db.add(ConsentLog(campaign_id=s.campaign_id, session_id=s.id, member_id=a.member_id,
                              user_id=m.user_id if m else None, guest_name=a.guest_name, action="on_site",
                              recorded_by_user_id=recorded_by.id, at=a.consent_at or utcnow()))


def check_upload_consent(db: Session, s: GameSession) -> None:
    """Vor jedem Upload: alle zugestimmt, und keine App-Zustimmung inzwischen widerrufen."""
    if not s.attendees or not all(a.consent for a in s.attendees):
        raise errors.conflict("consent_missing", "consent_missing.upload")
    for a in s.attendees:
        if a.consent_source == "app" and a.member_id:
            m = db.get(Member, a.member_id)
            if m is None or m.recording_consent_at is None:
                raise errors.conflict("consent_revoked", name=member_label(m) if m else "?")


def next_session_number(db: Session, campaign_id: str) -> int:
    current = db.scalar(select(func.max(GameSession.number)).where(GameSession.campaign_id == campaign_id))
    return (current or 0) + 1


def set_state(s: GameSession, state: str, progress: float | None = None, message: str | None = None) -> None:
    s.state = state
    s.progress = progress
    s.status_message = message
    s.state_updated_at = utcnow()


# ---------- Bibel ----------
def entry_out(e: Entry, is_gm: bool, viewer_id: str | None = None) -> schemas.EntryOut:
    """viewer_id: Mitglied, das abfragt – die Herkunft mitgebrachter Einträge (0.4.7) sehen nur SL und Urheberin."""
    mentions = [mn for mn in e.mentions if is_gm or mn.session.state == "published"]
    mentions.sort(key=lambda mn: mn.session.number)
    numbers = [mn.session.number for mn in mentions]
    daten = dict(
        id=e.id, campaign_id=e.campaign_id, type=e.type, name=e.name, summary=e.summary,
        status=e.status, holder_member_id=e.holder_member_id, visibility=e.visibility,
        first_session_number=min(numbers) if numbers else None,
        last_session_number=max(numbers) if numbers else None,
        mentions=[schemas.MentionOut(session_number=mn.session.number, note=mn.note) for mn in mentions],
        updated_at=e.updated_at,
        links=links.lesen(e.links, nur_geteilt=not is_gm),  # 0.4.13
    )
    if is_gm:
        daten["gm_notes"] = e.gm_notes
        daten["hidden_from_member_ids"] = sorted(e.hidden_member_ids)
        daten["former_holder_member_id"] = e.former_holder_member_id  # 0.4.9
    if is_gm or (viewer_id is not None and viewer_id == e.origin_member_id):
        daten.update(origin_character_id=e.origin_character_id, origin_entry_id=e.origin_entry_id,
                     origin_version=e.origin_version)
    return schemas.EntryOut(**daten)


_PUBLIC_FIELDS = ("type", "name", "summary", "status", "holder_member_id", "visibility")


def apply_entry_input(db: Session, e: Entry, data: schemas.EntryInput, require_public_text: bool = False) -> None:
    """Übernimmt gesetzte Felder und prüft die Regeln (status nur Quests, Halter nur Gegenstände).

    require_public_text: bei PATCH – wer einen Eintrag öffentlich macht, braucht einen öffentlichen Text (0.3.5).
    """
    vorher = {f: getattr(e, f) for f in _PUBLIC_FIELDS}
    vorher_verborgen = set(e.hidden_member_ids)
    vorher_geteilt = links.lesen(e.links, nur_geteilt=True)
    fields = data.model_fields_set
    if "type" in fields:
        if data.type is None:
            raise errors.bad_request("validation_error", "validation_error.type")
        if (data.type == "pc") != (e.type == "pc") and e.type:
            raise errors.bad_request("validation_error", "validation_error.pc")  # pc legt nur der Server an
        e.type = data.type
    if "name" in fields:
        name = (data.name or "").strip()
        if not name:
            raise errors.bad_request("validation_error", "validation_error.name")
        e.name = name
    if "summary" in fields:
        e.summary = data.summary or ""
    if "gm_notes" in fields:
        e.gm_notes = (data.gm_notes or "").strip() or None
    if "visibility" in fields:
        if data.visibility is None:
            raise errors.bad_request("validation_error", "validation_error.visibility")
        e.visibility = data.visibility
    if "status" in fields:
        e.status = data.status
    if "holder_member_id" in fields:
        if data.holder_member_id is not None:
            m = db.get(Member, data.holder_member_id)
            if m is None or m.campaign_id != e.campaign_id:
                raise errors.bad_request("holder_unknown")
        e.holder_member_id = data.holder_member_id
    if "hidden_from_member_ids" in fields:
        ids = set(data.hidden_from_member_ids or [])
        if ids:
            gueltig = set(db.scalars(select(Member.id).where(Member.campaign_id == e.campaign_id, Member.id.in_(ids))))
            if gueltig != ids:
                raise errors.bad_request("hidden_member_unknown")
        e.hidden_from = [EntryHidden(entry_id=e.id, member_id=m) for m in sorted(ids)]
    if "links" in fields and data.links is not None:  # 0.4.13: ganze Liste ersetzen
        e.links = links.schreiben(links.pruefen(data.links))
    if require_public_text and e.visibility == "public" and not (e.summary or "").strip():
        raise errors.bad_request("validation_error", "public_text_required")
    if e.type != "quest":
        e.status = None
    elif e.status is None:
        e.status = "active"
    if e.type not in ("item", "pc"):  # pc: Halter = Mitglied mit dem Charakter (0.4.7)
        e.holder_member_id = None
    now = utcnow()
    e.updated_at = now
    # „Neu“ für Spieler nur, wenn sich am öffentlichen Teil etwas geändert hat
    geaendert = (any(getattr(e, f) != vorher[f] for f in _PUBLIC_FIELDS) or set(e.hidden_member_ids) != vorher_verborgen
                 or links.lesen(e.links, nur_geteilt=True) != vorher_geteilt)
    if e.visibility == "public" and geaendert:
        e.public_changed_at = now


def search_matches(e: Entry, q: str, is_gm: bool) -> bool:
    """Volltextsuche: alle Suchwörter müssen vorkommen. gmNotes durchsucht nur die SL."""
    teile = [e.name, e.summary]
    if is_gm and e.gm_notes:
        teile.append(e.gm_notes)
    haystack = "\n".join(teile).casefold()
    return all(term in haystack for term in q.casefold().split())
