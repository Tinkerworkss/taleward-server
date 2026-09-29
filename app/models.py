"""Datenbanktabellen. IDs sind UUID-Strings, Zeiten immer UTC."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class ServerMeta(Base):
    """Einstellungen, die der Server selbst verwaltet (z. B. seine dauerhafte Server-ID für Tokens)."""

    __tablename__ = "server_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------- Konten
class User(Base):
    """Konto. Wie man sich anmeldet (Passwort, später OIDC/Passkey), steht getrennt in AuthMethod."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # immer kleingeschrieben gespeichert → Anmeldung unabhängig von Groß-/Kleinschreibung
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    # Wird beim Passwort-Zurücksetzen erhöht → alte Anmeldungen werden ungültig
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Einrichtungskonto admin/admin: nur für die Ersteinrichtung in der Verwaltung, löscht sich danach selbst
    setup_account: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Selbstregistrierung: Zustimmung zum Datenschutzhinweis und Altersbestätigung mit Zeitpunkt
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    age_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Freiwillige E-Mail nur für Kontozwecke (0.4.0): bestätigt bzw. eingetragen, aber noch nicht bestätigt
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, index=True)
    email_pending: Mapped[str | None] = mapped_column(String(254), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Nach „Passwort ändern“ in der App bleibt genau das Token gültig, mit dem geändert wurde („<ver>:<jti>“)
    token_ausnahme: Mapped[str | None] = mapped_column(String(80), nullable=True)

    auth_methods: Mapped[list["AuthMethod"]] = relationship(cascade="all, delete-orphan")


class AuthMethod(Base):
    __tablename__ = "auth_methods"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # password | oidc:google | oidc:discord | … | passkey
    secret: Mapped[str] = mapped_column(Text)  # bei password: Argon2-Hash; bei oidc:*: Kennung beim Dienst (sub)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ---------------------------------------------------------------- Organisation
class Organization(Base):
    """Oberste Ebene: Verein, Laden, Gruppe. Ein selbst betriebener Server hat meist genau eine."""

    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OrgMember(Base):
    __tablename__ = "org_members"
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- Kampagnen
class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(8), default="de")
    system: Mapped[str | None] = mapped_column(String(32), nullable=True)
    system_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    world_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cover_image_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_session_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # abgeschlossen (0.4.5)
    # darf der externe Anbieter (falls der Betreiber einen freigibt) für diese Kampagne transkribieren?
    allow_external_transcription: Mapped[bool] = mapped_column(Boolean, default=False)
    # 0.3.10: Recap/Vorschläge (und Unterlagen) über die Cloud-API nur, wenn die SL es erlaubt
    allow_cloud_summary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    members: Mapped[list["Member"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    organization: Mapped[Organization | None] = relationship()


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    # None = Konto gelöscht: Das Mitglied bleibt als „Gelöschtes Konto“ stehen, damit Kapitel, Anwesende und
    # Kommentare stimmig bleiben; Bild, Hintergrund und Zustimmung sind dann entfernt.
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Verlassen oder entfernt (0.4.5): bleibt stehen wie ein gelöschtes Konto, user_id bleibt für die Rückkehr
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    role: Mapped[str] = mapped_column(String(16))  # gm | player
    character_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    character_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # nur die Person selbst und die SL; fließt nie in die KI-Verarbeitung
    character_backstory: Mapped[str | None] = mapped_column(Text, nullable=True)
    portrait_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    recording_consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    chronicle_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    bible_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship()

    @property
    def aktiv(self) -> bool:
        return self.user_id is not None and self.left_at is None

    @property
    def anzeigename(self) -> str:
        """Anzeigename des Kontos; nach Kontolöschung „Gelöschtes Konto“ in der Sprache der Kampagne."""
        if self.user is not None:
            return self.user.display_name
        return "Deleted account" if self.campaign.language == "en" else "Gelöschtes Konto"


class ConsentLog(Base):
    """Nachweis jeder Zustimmung und jedes Widerrufs (bleibt auch nach Kontolöschung bestehen)."""

    __tablename__ = "consent_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)  # bewusst ohne Fremdschlüssel
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # granted | revoked (stehende Zustimmung in der App) | on_site (vor Ort für eine Session)
    action: Mapped[str] = mapped_column(String(16))
    recorded_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------- Sessions
class GameSession(Base):
    """Eine gespielte Session = ein Kapitel."""

    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("campaign_id", "number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    played_at: Mapped[datetime] = mapped_column(UTCDateTime)
    state: Mapped[str] = mapped_column(String(32), default="created", index=True)
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    transcription_engine: Mapped[str | None] = mapped_column(String(16), nullable=True)  # local | external
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    state_updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    attendees: Mapped[list["Attendee"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Attendee.position"
    )


class Attendee(Base):
    """Anwesende Person: Mitglied (member_id) oder Gast ohne Konto (guest_name)."""

    __tablename__ = "attendees"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[str | None] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str] = mapped_column(String(16), default="on_site")  # app | on_site
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    session: Mapped[GameSession] = relationship(back_populates="attendees")


class SessionSeen(Base):
    """Gelesen-Marker pro Mitglied und Kapitel (für ungelesene Kommentare)."""

    __tablename__ = "session_seen"
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class GmNote(Base):
    __tablename__ = "gm_notes"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- Bibel
class Entry(Base):
    """Eintrag der Kampagnenbibel."""

    __tablename__ = "entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    # geheimer Teil, auch bei öffentlichen Einträgen – Spieler sehen ihn nie
    gm_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    holder_member_id: Mapped[str | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(16), default="gm_only")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Zeitpunkt, ab dem Spieler den Eintrag „neu“ sehen (öffentlich geworden oder öffentlich geändert)
    public_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    mentions: Mapped[list["EntryMention"]] = relationship(cascade="all, delete-orphan")
    hidden_from: Mapped[list["EntryHidden"]] = relationship(cascade="all, delete-orphan")

    @property
    def hidden_member_ids(self) -> set[str]:
        return {h.member_id for h in self.hidden_from}


class EntryHidden(Base):
    """„Wer weiß was“: öffentlicher Eintrag, der vor einzelnen Mitgliedern verborgen ist."""

    __tablename__ = "entry_hidden"
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)


class EntryMention(Base):
    """In welcher Session ein Eintrag vorkam (wird beim Veröffentlichen befüllt)."""

    __tablename__ = "entry_mentions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    session: Mapped[GameSession] = relationship()


# ---------------------------------------------------------------- Stimmprofil
class VoiceProfile(Base):
    """Stimmprofil – enthält später nur den Stimmabdruck, nie Audio."""

    __tablename__ = "voice_profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="none")
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sample_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    learn_from_sessions: Mapped[bool] = mapped_column(Boolean, default=False)
    learned_session_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Biometrisch, verlässt den Server nie. Nur Zahlen, nie Audio.
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    base_embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, aus der eigenen Aufnahme
    learned_sum: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, Summe gelernter Abdrücke
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)


# ---------------------------------------------------------------- Upload
class Upload(Base):
    """Ein Upload-Vorgang für eine Session (Teile liegen auf der Platte unter data/uploads/<id>/)."""

    __tablename__ = "uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # table | discord
    state: Mapped[str] = mapped_column(String(16), default="open")  # open | completed | aborted
    chunk_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    files: Mapped[list["UploadFile"]] = relationship(
        cascade="all, delete-orphan", order_by="UploadFile.position"
    )


class UploadFile(Base):
    __tablename__ = "upload_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(100))
    track_member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- Warteschlange
class Worker(Base):
    """Ein Worker (z. B. PC mit Grafikkarte). Meldet sich mit eigenem Token, nicht mit einem Nutzerkonto."""

    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64))
    capabilities: Mapped[str] = mapped_column(String(200), default="asr,llm")  # kommagetrennt
    info: Mapped[str | None] = mapped_column(Text, nullable=True)  # zuletzt gemeldete Angaben (JSON)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # nimmt keine Aufträge an
    local: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # von der Verwaltung gestartet


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(16))  # transcribe | summarize | document | voice_enroll
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    upload_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # voice_enroll
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued | leased | done | failed
    required_capability: Mapped[str] = mapped_column(String(16))  # asr | llm | embed
    engine: Mapped[str] = mapped_column(String(16), default="local")  # local | external
    lease_worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------- Ergebnis der Transkription
class Speaker(Base):
    """Erkannte Stimme einer Session. Hörprobe und Stimmabdruck bleiben nur bis zur bestätigten Zuordnung."""

    __tablename__ = "speakers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(50))
    raw_label: Mapped[str] = mapped_column(String(50))  # Kennung des Workers, z. B. SPEAKER_01
    speaking_seconds: Mapped[float] = mapped_column(Float, default=0)
    sample_text: Mapped[str] = mapped_column(Text, default="")
    sample_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-Liste, biometrisch – kurzlebig
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Modell@Fassung
    suggested_member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(16), default="none")
    assigned_member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    speaker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    text: Mapped[str] = mapped_column(Text)


class UsageLog(Base):
    """Verbrauch pro Verarbeitung – Grundlage für GET /campaigns/{id}/usage und spätere Abrechnung."""

    __tablename__ = "usage_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # transcription | summary | document
    engine: Mapped[str] = mapped_column(String(16), default="local")
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0)
    compute_seconds: Mapped[float] = mapped_column(Float, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    billed_to: Mapped[str] = mapped_column(String(16), default="gm")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- Ergebnis der Zusammenfassung
class Recap(Base):
    """Recap einer Session. Für Spieler erst sichtbar, wenn die Session veröffentlicht ist."""

    __tablename__ = "recaps"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    open_threads: Mapped[str] = mapped_column(Text, default="[]")  # JSON-Liste
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # von der SL bearbeitet
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Proposal(Base):
    """Bibel-Vorschlag aus einer Session (später auch aus SL-Unterlagen). Spieler sehen Vorschläge nie."""

    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    entry_type: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))  # create | update | reveal
    target_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    gm_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_visibility: Mapped[str] = mapped_column(String(16), default="gm_only")
    hidden_member_ids_json: Mapped[str] = mapped_column("hidden_member_ids", Text, default="[]")
    public_suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    flags: Mapped[str] = mapped_column(Text, default="[]")  # JSON-Liste
    evidence: Mapped[str] = mapped_column(Text, default="[]")  # JSON-Liste {start|page, quote}
    decision: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- SL-Unterlagen
class CampaignDocument(Base):
    """Hochgeladene Unterlage der SL (PDF, Word, Text). Nur die SL sieht sie. Der ausgelesene Text liegt neben der
    Datei (data/unterlagen/<id>/text.json), die Vorschläge in `proposals` (document_id)."""

    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    file_name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(16))  # handout | gm | mixed
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="queued")  # queued|processing|awaiting_review|done|failed
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-Schlüssel wie status_message
    world_info_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by_member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- Miteinander (Kommentare, Termine)
class Comment(Base):
    """Kommentar zu einem Kapitel: öffentlich (recipient None) oder privat zwischen zwei Mitgliedern.
    Fließt nie in Recaps oder Vorschläge ein."""

    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    author_member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    recipient_member_id: Mapped[str | None] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class DatePoll(Base):
    __tablename__ = "date_polls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | closed | cancelled
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    chosen_option_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    options: Mapped[list["DateOption"]] = relationship(cascade="all, delete-orphan", order_by="DateOption.starts_at")


class DateOption(Base):
    __tablename__ = "date_options"
    __table_args__ = (UniqueConstraint("poll_id", "starts_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    poll_id: Mapped[str] = mapped_column(ForeignKey("date_polls.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    proposed_by_member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    votes: Mapped[list["DateVote"]] = relationship(cascade="all, delete-orphan", order_by="DateVote.created_at")


class DateVote(Base):
    __tablename__ = "date_votes"
    option_id: Mapped[str] = mapped_column(ForeignKey("date_options.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    answer: Mapped[str] = mapped_column(String(8))  # yes | maybe | no
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------- Einmal-Tokens (0.4.0)
class EinmalToken(Base):
    """Kurzlebige Einmal-Werte: E-Mail bestätigen, Anmeldung mit Dienst (Zustand, Ticket, Registrierung, Verbinden).
    Gespeichert wird nur die SHA-256 des Werts; nach Gebrauch oder Ablauf gelöscht."""

    __tablename__ = "einmal_tokens"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA-256 hex des Werts
    art: Mapped[str] = mapped_column(String(24), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    daten: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
