"""Pydantic-Modelle, Feldnamen 1:1 wie in contract/session-chronik-api.yaml (camelCase).

Hinweis: Einige Felder werden je nach Betrachter WEGGELASSEN (nicht null gesetzt), z. B.
characterBackstory und gmNotes für Spieler. Die betroffenen Endpunkte antworten deshalb mit
response_model_exclude_unset=True, und die Ausgabe-Funktionen in services.py setzen jedes andere
Feld ausdrücklich.
"""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

Role = Literal["gm", "player"]
Visibility = Literal["public", "gm_only"]
EntryType = Literal["npc", "location", "quest", "item", "faction", "other", "pc"]  # pc ab 0.4.7, nur vom Server
UUID_MUSTER = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
CharacterStatus = Literal["active", "retired", "deceased"]
ContentLanguage = Literal["de", "en"]
GameSystem = Literal["dsa", "dnd", "pathfinder", "cthulhu", "shadowrun", "splittermond", "other"]
CoverPreset = Literal["meadow", "forest", "desert", "city", "cyber", "mountains", "coast", "swamp",
                      "dungeon", "space", "castle",
                      # ab 0.4.3
                      "dark-fantasy", "moonwood", "ancient-ruins", "tavern", "battlefield", "frozen-north",
                      "arcane-ruins", "fairy-wilds", "underworld", "storm-coast", "steampunk", "post-apocalypse",
                      "western", "noir", "space-opera", "orient", "necropolis", "manor", "riverside-mystery"]
COVER_PRESETS: tuple[str, ...] = CoverPreset.__args__  # type: ignore[attr-defined]
ProcessingState = Literal[
    "created", "uploading", "queued", "transcribing", "awaiting_speakers",
    "summarizing", "awaiting_review", "published", "failed",
]


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def _as_utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


# ---------- Server / Auth ----------
class ServerInfoOut(ApiModel):
    name: str
    operator: str
    contact: str | None
    api_version: str
    registration: Literal["invite_only", "closed"]
    auth_methods: list[Literal["password", "oidc", "passkey"]]
    privacy_policy_url: str | None
    min_age: int
    min_app_version: str | None = None
    latest_app_version: str | None = None
    app_download_url: str | None = None
    release_notes: str | None = None
    app_download_sha256: str | None = None
    app_download_size_bytes: int | None = None
    external_transcription: str | None
    external_transcription_mode: Literal["fallback", "primary"] | None = None
    cloud_summary: str | None = None
    cloud_summary_info: "CloudProviderInfoOut | None" = None  # 0.4.11
    external_transcription_info: "CloudProviderInfoOut | None" = None  # 0.4.11
    auth_providers: list["AuthProviderOut"] = []
    password_reset: bool = False
    audio_retention: "AudioRetentionOut | None" = None


class InvitePreviewOut(ApiModel):
    """0.4.10: Vorschau einer Einladung ohne Anmeldung – nur, was /einladung/{code} ohnehin zeigt."""
    campaign_title: str
    seat_character_name: str | None = None
    expires_at: datetime | None = None


class CloudProviderInfoOut(ApiModel):
    """0.4.11: Wer Text (Zusammenfassung) bzw. Aufnahmen (Transkription) verarbeitet – ohne sprachabhängige Wörter."""
    id: str
    name: str
    region: Literal["eu", "non_eu"]
    country: str | None = None


class AudioRetentionOut(ApiModel):
    mode: Literal["until_release", "immediate"]
    max_days: int | None


class AuthProviderOut(ApiModel):
    id: Literal["google", "discord", "apple", "microsoft"]
    name: str


class LoginRequest(ApiModel):
    username: str
    password: str


class UserOut(ApiModel):
    """Nur für die Person selbst (Login, /me, Registrierung, Tausch) – nie für andere."""
    id: str
    username: str
    display_name: str
    email: str | None = None
    email_pending: str | None = None
    has_password: bool = True
    providers: list[Literal["google", "discord", "apple", "microsoft"]] = []


class RegisterRequest(ApiModel):
    """Zwei Formen (0.4.0): mit username + password oder mit registrationToken aus /auth/oidc/exchange."""
    invite_code: str = Field(max_length=64)
    username: str | None = Field(default=None, max_length=200)
    display_name: str = Field(max_length=300)
    password: str | None = Field(default=None, max_length=500)
    registration_token: str | None = Field(default=None, max_length=200)
    accept_privacy: bool = False
    age_confirmed: bool = False


class DeleteCampaignRequest(ApiModel):
    confirm_title: str = Field(max_length=300)


class DeleteMeRequest(ApiModel):
    password: str | None = Field(default=None, max_length=500)
    confirm_username: str | None = Field(default=None, max_length=200)


class PasswordResetRequest(ApiModel):
    login: str = Field(max_length=300)


class EmailRequest(ApiModel):
    email: str = Field(max_length=300)


class PasswordChangeRequest(ApiModel):
    current_password: str | None = Field(default=None, max_length=500)
    new_password: str = Field(max_length=500)


class OidcExchangeRequest(ApiModel):
    ticket: str = Field(max_length=200)
    verifier: str = Field(max_length=200)


class OidcExchangeResponse(ApiModel):
    status: Literal["ok", "register", "email_in_use"]
    access_token: str | None = None
    expires_at: datetime | None = None
    user: UserOut | None = None
    registration_token: str | None = None
    suggested_display_name: str | None = None
    email: str | None = None
    provider: str | None = None


class LinkTokenOut(ApiModel):
    link_token: str


class LoginResponse(ApiModel):
    access_token: str
    expires_at: datetime
    user: UserOut


class OrganizationOut(ApiModel):
    id: str
    name: str
    my_role: Literal["admin", "member"]


class OrgRef(ApiModel):
    id: str
    name: str


# ---------- Stimmprofil ----------
class VoiceProfileOut(ApiModel):
    status: Literal["none", "processing", "ready", "failed"]
    created_at: datetime | None
    sample_seconds: float | None
    learn_from_sessions: bool
    learned_session_count: int
    message: str | None


class VoiceProfilePatch(ApiModel):
    learn_from_sessions: bool | None = None


# ---------- Kampagnen ----------
class MemberOut(ApiModel):
    id: str
    user_id: str
    display_name: str
    character_name: str | None
    role: Role
    recording_consent_at: datetime | None
    character_summary: str | None
    character_backstory: str | None = None  # wird für Unbefugte weggelassen
    portrait_updated_at: datetime | None
    deleted_at: datetime | None = None
    left_at: datetime | None = None
    character_id: str | None = None  # 0.4.7
    character_version: int | None = None
    character_status: CharacterStatus | None = None
    character_nickname: str | None = None
    move_consent_at: datetime | None = None  # 0.4.8
    open_seat: bool = False


class UnreadOut(ApiModel):
    recaps: int
    comments: int
    bible: int


class CampaignSummaryOut(ApiModel):
    id: str
    title: str
    organization: OrgRef | None
    my_role: Role
    my_character_name: str | None
    member_count: int
    archived_at: datetime | None = None
    published_session_count: int
    pending_review_count: int
    open_character_proposals: int = 0  # 0.4.7, nur für die SL befüllt
    last_published_at: datetime | None
    cover_preset: CoverPreset | None
    unread: UnreadOut
    cover_image_updated_at: datetime | None
    next_session_at: datetime | None
    date_poll_needs_my_vote: bool


class Link(ApiModel):
    """0.4.13: Verweis nach draußen (Prüfung der Adresse in app/links.py)."""
    id: str = Field(pattern=UUID_MUSTER)
    label: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=1, max_length=2000)
    shared: bool = False


class CampaignOut(CampaignSummaryOut):
    description: str
    language: ContentLanguage
    system: GameSystem | None
    system_name: str | None
    world_info: str | None
    allow_external_transcription: bool
    allow_cloud_summary: bool = False
    hotwords: list[str] | None = None  # 0.4.6: nur für die SL, für Spieler weggelassen
    links: list[Link] = []  # 0.4.13: Spieler nur shared
    gm_notices: list["GmNoticeOut"] | None = None  # 0.4.7: nur für die SL, für Spieler weggelassen
    members: list[MemberOut]


class GmNoticeOut(ApiModel):
    id: str
    code: Literal["hidden_entries_for_newcomer", "seat_claimed", "character_orphaned"]
    member_id: str | None
    entry_ids: list[str]
    created_at: datetime


class CharacterIn(ApiModel):
    """Serverkopie eines Charakters aus der Sammlung der App (0.4.7)."""
    id: str = Field(pattern=UUID_MUSTER)
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    nickname: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, max_length=2000)
    backstory: str | None = Field(default=None, max_length=20000)
    system: str | None = Field(default=None, max_length=64)
    status: CharacterStatus
    status_changed_at: datetime | None = None


class WorldEntryIn(ApiModel):
    id: str = Field(pattern=UUID_MUSTER)
    version: int = Field(ge=1)
    type: Literal["npc", "location", "faction", "item", "quest", "other"]
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    secret: bool


class WorldIn(ApiModel):
    entries: list[WorldEntryIn]  # 1–100, geprüft im Endpunkt (400 world_too_many)


class WorldEntryStatusOut(ApiModel):
    id: str
    proposal_id: str | None
    entry_id: str | None
    state: Literal["pending", "accepted", "rejected", "unchanged"]
    server_version: int | None


class WorldOut(ApiModel):
    entries: list[WorldEntryStatusOut]


class CampaignCreate(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    language: ContentLanguage = "de"
    organization_id: str | None = None
    system: GameSystem | None = None
    system_name: str | None = Field(default=None, max_length=100)


class CampaignPatch(ApiModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    world_info: str | None = Field(default=None, max_length=50000)
    language: ContentLanguage | None = None
    system: GameSystem | None = None
    system_name: str | None = Field(default=None, max_length=100)
    cover_preset: CoverPreset | None = None
    allow_external_transcription: bool | None = None
    allow_cloud_summary: bool | None = None
    archived: bool | None = None
    hotwords: list[Annotated[str, Field(max_length=40)]] | None = Field(default=None, max_length=200)
    links: list[Link] | None = Field(default=None, max_length=20)  # 0.4.13: ganze Liste ersetzen


class JoinRequest(ApiModel):
    code: str = Field(min_length=1, max_length=32)
    character_name: str | None = Field(default=None, max_length=200)
    character: CharacterIn | None = None  # 0.4.7


class InviteOut(ApiModel):
    code: str
    expires_at: datetime


class SeatInviteOut(InviteOut):
    member_id: str  # 0.4.8: Einladung für genau diesen offenen Platz


# ---------- Umzug (0.4.8) ----------
class MoveConsentIn(ApiModel):
    granted: bool


class CampaignExportOut(ApiModel):
    id: str
    state: Literal["queued", "processing", "ready", "failed"]
    progress: float | None
    size_bytes: int | None
    expires_at: datetime | None
    download_url: str | None
    consented_member_ids: list[str]
    message: str | None
    created_at: datetime


class ImportStartIn(ApiModel):
    file_name: str = Field(min_length=1, max_length=300)
    size_bytes: int = Field(ge=1)


class ImportStartOut(ApiModel):
    import_id: str
    chunk_size_bytes: int
    chunk_count: int


class ImportStatusOut(ApiModel):
    id: str
    state: Literal["uploading", "processing", "done", "failed"]
    progress: float | None
    missing_chunks: list[int] | None = None  # nur bei uploading, sonst weggelassen
    campaign_id: str | None
    open_seats: int | None
    message: str | None


class MemberPatch(ApiModel):
    character_name: str | None = Field(default=None, max_length=200)
    character_summary: str | None = Field(default=None, max_length=1000)
    character_backstory: str | None = Field(default=None, max_length=8000)
    role: Role | None = None


class RecordingConsentIn(ApiModel):
    granted: bool


class SeenIn(ApiModel):
    area: Literal["chronicle", "bible"]


class UsageOut(ApiModel):
    month: str
    sessions: int
    audio_seconds: int
    documents: int
    cost_estimate_cents: int | None
    billed_to: Literal["gm", "organization"]


# ---------- Sessions ----------
class AttendeeIn(ApiModel):
    member_id: str | None = None
    guest_name: str | None = Field(default=None, max_length=200)
    consent: bool
    consent_source: Literal["app", "on_site"]
    consent_at: datetime | None = None

    _utc = field_validator("consent_at")(_as_utc)


class AttendeeOut(ApiModel):
    member_id: str | None = None  # genau eines von beiden wird ausgegeben
    guest_name: str | None = None
    consent: bool
    consent_source: Literal["app", "on_site"]
    consent_at: datetime | None


class SessionCreate(ApiModel):
    title: str | None = Field(default=None, max_length=300)
    played_at: datetime
    attendees: list[AttendeeIn]

    _utc = field_validator("played_at")(_as_utc)


class SessionPatch(ApiModel):
    title: str | None = Field(default=None, max_length=300)
    attendees: list[AttendeeIn] | None = None


class SessionSummaryOut(ApiModel):
    id: str
    number: int
    title: str | None
    played_at: datetime
    state: ProcessingState
    unread_comments: int


class SessionOut(SessionSummaryOut):
    campaign_id: str
    source: Literal["table", "discord"] | None
    transcription_engine: Literal["local", "external"] | None
    attendees: list[AttendeeOut]
    duration_seconds: int | None
    audio_deleted_at: datetime | None
    published_at: datetime | None


class ProcessingStatusOut(ApiModel):
    state: ProcessingState
    progress: float | None
    queue_position: int | None
    message: str | None
    updated_at: datetime
    estimated_seconds: int | None = None


class GmNoteOut(ApiModel):
    text: str
    updated_at: datetime


class GmNoteIn(ApiModel):
    text: str = Field(max_length=100000)


class SpeakerOut(ApiModel):
    id: str
    label: str
    speaking_seconds: float
    sample_text: str
    suggested_member_id: str | None
    confidence: float
    source: Literal["intro_round", "voice_match", "discord_track", "none"]
    assigned_guest_name: str | None = None  # 0.4.7
    assigned_member_id: str | None = None  # 0.4.10: erst nach der Bestätigung, vorher weggelassen


class SpeakerAssignIn(ApiModel):
    speaker_id: str
    member_id: str | None = None  # null = Gast (mit guestName) / ignorieren
    guest_name: str | None = Field(default=None, max_length=200)  # 0.4.7


class RecapOut(ApiModel):
    session_id: str
    number: int
    title: str
    text: str
    open_threads: list[str]
    published_at: datetime | None
    review: dict | None = None  # 0.4.6: Prüfteil, nur für die SL (für Spieler weggelassen)


class RecapIn(ApiModel):
    title: str | None = Field(default=None, max_length=300)
    text: str | None = Field(default=None, max_length=100000)
    open_threads: list[str] | None = Field(default=None, max_length=50)


class EvidenceOut(ApiModel):
    start: float | None = None
    page: int | None = None
    quote: str


class ProposalOut(ApiModel):
    id: str
    session_id: str | None
    document_id: str | None
    source: Literal["session", "document", "character"]  # 0.4.7
    origin_character_id: str | None = None
    origin_entry_id: str | None = None
    submitted_by_member_id: str | None = None
    entry_type: EntryType
    action: Literal["create", "update", "reveal"]
    target_entry_id: str | None
    title: str
    detail: str
    gm_notes: str | None
    suggested_visibility: Visibility
    hidden_from_member_ids: list[str]
    public_suggested: bool
    visibility_reason: str | None
    confidence: float
    flags: list[Literal["joke_suspected", "low_confidence", "contradicts_bible", "evidence_not_found"]]
    evidence: list[EvidenceOut]
    decision: Literal["open", "accepted", "rejected"]


class ProposalPatch(ApiModel):
    decision: Literal["open", "accepted", "rejected"] | None = None
    title: str | None = Field(default=None, max_length=300)
    detail: str | None = Field(default=None, max_length=20000)
    gm_notes: str | None = Field(default=None, max_length=20000)
    visibility: Visibility | None = None
    hidden_from_member_ids: list[str] | None = None


class TranscriptLineOut(ApiModel):
    start: float
    end: float
    speaker_id: str
    member_id: str | None
    text: str


# ---------- Bibel ----------
class EntryInput(ApiModel):
    type: EntryType | None = None
    name: str | None = Field(default=None, max_length=300)
    summary: str | None = Field(default=None, max_length=20000)
    status: Literal["active", "done"] | None = None
    holder_member_id: str | None = None
    visibility: Visibility | None = None
    gm_notes: str | None = Field(default=None, max_length=20000)
    hidden_from_member_ids: list[str] | None = None
    links: list[Link] | None = Field(default=None, max_length=3)  # 0.4.13


class MentionOut(ApiModel):
    session_number: int
    note: str


class EntryOut(ApiModel):
    id: str
    campaign_id: str
    type: EntryType
    name: str
    summary: str
    status: Literal["active", "done"] | None
    holder_member_id: str | None
    visibility: Visibility
    gm_notes: str | None = None  # wird für Spieler weggelassen
    hidden_from_member_ids: list[str] | None = None  # wird für Spieler weggelassen
    first_session_number: int | None
    last_session_number: int | None
    mentions: list[MentionOut]
    updated_at: datetime
    origin_character_id: str | None = None  # 0.4.7: nur für SL und Urheberin, sonst weggelassen
    origin_entry_id: str | None = None
    origin_version: int | None = None
    former_holder_member_id: str | None = None  # 0.4.9: nur für die SL, sonst weggelassen
    links: list[Link] = []  # 0.4.13: Spieler nur shared


class EntryAssignIn(ApiModel):
    """0.4.9: Figur einem anderen Spieler geben."""
    member_id: str = Field(max_length=36)


# ---------- Kommentare ----------
class CommentOut(ApiModel):
    id: str
    session_id: str
    author_member_id: str
    recipient_member_id: str | None
    text: str
    created_at: datetime
    edited_at: datetime | None


class CommentCreate(ApiModel):
    text: str = ""  # Länge prüft der Endpunkt selbst (413 comment_too_long statt 400)
    recipient_member_id: str | None = None


class CommentPatch(ApiModel):
    text: str = ""


# ---------- Terminabstimmung ----------
VoteAnswer = Literal["yes", "maybe", "no"]


class DateVoteOut(ApiModel):
    member_id: str
    answer: VoteAnswer


class DateOptionOut(ApiModel):
    id: str
    starts_at: datetime
    proposed_by_member_id: str
    votes: list[DateVoteOut]


class DatePollOut(ApiModel):
    id: str
    campaign_id: str
    status: Literal["open", "closed", "cancelled"]
    note: str | None
    options: list[DateOptionOut]
    chosen_option_id: str | None
    created_at: datetime


class DatePollCreate(ApiModel):
    note: str | None = Field(default=None, max_length=1000)


class DateOptionCreate(ApiModel):
    starts_at: datetime

    @field_validator("starts_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)


class DateVoteIn(ApiModel):
    answer: VoteAnswer


class DatePollClose(ApiModel):
    option_id: str


# ---------- SL-Unterlagen ----------
DocumentKind = Literal["handout", "gm", "mixed", "character_sheet"]


class CampaignDocumentOut(ApiModel):
    id: str
    campaign_id: str
    title: str
    file_name: str
    kind: DocumentKind
    size_bytes: int
    page_count: int | None
    state: Literal["queued", "processing", "awaiting_review", "done", "failed"]
    progress: float | None
    message: str | None
    proposal_count: int
    open_proposal_count: int
    world_info_suggestion: str | None
    uploaded_by_member_id: str | None = None  # 0.4.7
    created_at: datetime


class DocumentApplyIn(ApiModel):
    apply_world_info: bool = False


# ---------------------------------------------------------------- Kapitelplan (0.4.12)
class DocumentPageOut(ApiModel):
    page: int
    text: str


class DocumentTextOut(ApiModel):
    pages: list[DocumentPageOut]


class PlanScene(ApiModel):
    id: str = Field(pattern=UUID_MUSTER)
    title: str = Field(min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)
    entry_ids: list[Annotated[str, Field(max_length=36)]] = Field(default=[], max_length=30)
    state: Literal["open", "played", "skipped"] = "open"
    links: list[Link] = Field(default=[], max_length=3)  # 0.4.13


class ChapterPlanIn(ApiModel):
    """POST: title ist Pflicht. PATCH (ChapterPlanPatchIn): nur mitgeschickte Felder ändern sich."""
    title: str = Field(min_length=1, max_length=120)
    session_number: int | None = Field(default=None, ge=1)
    state: Literal["draft", "ready", "played"] = "draft"
    notes: str | None = Field(default=None, max_length=20000)
    table_notes: str | None = Field(default=None, max_length=20000)  # 0.4.13
    scenes: list[PlanScene] = Field(default=[], max_length=50)
    names: list[Annotated[str, Field(max_length=40)]] = Field(default=[], max_length=100)
    document_ids: list[Annotated[str, Field(max_length=36)]] = Field(default=[], max_length=20)


class ChapterPlanPatchIn(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    session_number: int | None = Field(default=None, ge=1)
    state: Literal["draft", "ready", "played"] | None = None
    notes: str | None = Field(default=None, max_length=20000)
    table_notes: str | None = Field(default=None, max_length=20000)  # 0.4.13
    scenes: list[PlanScene] | None = Field(default=None, max_length=50)
    names: list[Annotated[str, Field(max_length=40)]] | None = Field(default=None, max_length=100)
    document_ids: list[Annotated[str, Field(max_length=36)]] | None = Field(default=None, max_length=20)
    if_updated_at: datetime | None = None


class ChapterPlanOut(ApiModel):
    id: str
    campaign_id: str
    title: str
    session_number: int | None
    state: Literal["draft", "ready", "played"]
    notes: str | None
    table_notes: str | None = None  # 0.4.13
    scenes: list[PlanScene]
    names: list[str]
    document_ids: list[str]
    created_at: datetime
    updated_at: datetime


# ---------- Qualitätsprüfung (0.4.6) ----------
class CorrectionIn(ApiModel):
    heard: str = Field(min_length=1, max_length=100)
    correct: str = Field(max_length=100)
    add_to_hotwords: bool = True


class CorrectionsIn(ApiModel):
    corrections: list[CorrectionIn] = Field(min_length=1, max_length=100)
    retranscribe: bool = False


CampaignOut.model_rebuild()
