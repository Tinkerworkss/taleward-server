from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jwt_secret: str = "bitte-aendern"
    jwt_expire_days: int = 30
    data_dir: Path = Path("./data")
    cors_origins: str = "http://localhost:5173,http://localhost"
    invite_ttl_days: int = 7
    registration: str = "invite_only"  # invite_only = Konto per Einladungscode selbst anlegen | closed
    forwarded_allow_ips: str = "127.0.0.1"
    # Angaben für GET /info (öffentlich sichtbar in der App)
    server_name: str = "Session-Chronik"
    server_operator: str = "Rollenspielverein"
    server_contact: str | None = None
    privacy_policy_url: str | None = None
    min_age: int = 16
    # App-Versionen (0.3.9) – normalerweise in der Verwaltung gesetzt
    app_min_version: str | None = None
    app_latest_version: str | None = None
    app_download_url: str | None = None
    app_release_notes: str | None = None
    public_url: str | None = None  # öffentliche Adresse (https://…), für Links in E-Mails und Anmeldedienste
    # Name der Organisation, die beim ersten Start angelegt wird
    organization_name: str = "Rollenspielverein"
    # Upload und Warteschlange (Schritt 2)
    chunk_size_bytes: int = 5 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024 * 1024  # je Datei; 5 h Opus/AAC liegen weit darunter
    audio_retention_days: int = 7  # Audio fehlgeschlagener Sessions wird spätestens danach gelöscht
    upload_abandon_days: int = 7  # nie abgeschlossene Uploads werden danach verworfen
    lease_seconds: int = 180  # so lange gilt ein Auftrag ohne Lebenszeichen als vergeben
    max_attempts: int = 3
    claim_wait_seconds: int = 25  # Long-Poll beim Abholen von Aufträgen
    worker_offline_after_seconds: int = 120
    maintenance_interval_seconds: int = 30  # 0 = keine automatische Wartung (Tests)
    # Zusammenfassung (Recap + Bibel-Vorschläge) in der Zentrale. Schritt 2b: "attrappe" (Platzhaltertext),
    # ab Schritt 5 ein echtes Sprachmodell. "aus" = Aufträge warten.
    summarizer: str = "attrappe"  # aus | attrappe | lokal (Worker mit Ollama) | api
    llm_api_url: str = "https://api.mistral.ai/v1"  # OpenAI-kompatibel
    llm_api_model: str = "mistral-large-latest"
    llm_api_key: str | None = None  # leer + Mistral: Schlüssel der externen Transkription
    llm_local_model: str = "ministral-3:8b"
    llm_local_context: int = 12288
    worker_llm_url: str = "http://localhost:11434"  # Ollama auf dem Worker
    summarizer_interval_seconds: float = 2.0  # 0 = kein eigener Arbeitsprozess (Tests)
    create_setup_account: bool = True  # bei leerer Datenbank Einrichtungskonto admin/admin anlegen
    local_worker_autostart: bool = True  # lokalen Worker mitstarten, falls in der Verwaltung eingestellt
    # Nur für den Worker (chronik worker)
    worker_server_url: str = "http://127.0.0.1:8000"
    worker_token: str | None = None
    worker_work_dir: Path = Path("./data/worker")
    # Hugging Face Zugangsschlüssel (für das pyannote-Sprechermodell)
    hf_token: str | None = None
    # Taleward-Spiegel des Sprechermodells (ohne Hugging-Face-Konto). Genutzt nur, wenn die Prüfsumme im Code steht.
    model_mirror_url: str = ""
    # Updates: Der Server fragt einmal am Tag bei GitHub nach neuen Fassungen von App, Worker und Server, lädt die
    # Dateien für App und Worker und bietet sie selbst an – so fragen Handys und Worker nie direkt bei GitHub.
    update_check: bool = True
    update_app_repo: str = "Tinkerworkss/taleward-app"
    update_server_repo: str = "Tinkerworkss/taleward-server"
    update_worker_repo: str = "Tinkerworkss/taleward-server"  # nach dem Umzug der Worker-App: eigenes Repository
    # Externe Transkription als Ersatz (Standard aus). Audio verlässt dann den eigenen Betrieb →
    # Vertrag zur Auftragsverarbeitung mit dem Anbieter und Absatz im Datenschutzhinweis nötig.
    external_transcription: str = ""  # "mistral" = freigegeben
    mistral_api_key: str | None = None
    external_model: str = "voxtral-mini-latest"
    external_api_url: str = "https://api.mistral.ai/v1/audio/transcriptions"
    external_after_hours: float = 24.0  # so lange muss ein Auftrag ohne erreichbaren Worker warten
    external_max_seconds: float = 9000.0  # längere Aufnahmen werden geteilt (Anbieter: bis 3 h je Anfrage)
    external_cost_cents_per_minute: float = 0.3  # Schätzung für den Verbrauch (≈ 0,003 $ pro Minute)
    external_interval_seconds: float = 60.0  # 0 = kein eigener Prozess (Tests)
    # Worker mit Grafikkarte (Schritt 3). Bei wenig Grafikspeicher: WHISPER_BATCH=4 oder large-v3-turbo
    whisper_model: str = "large-v3"
    whisper_compute_type: str = "int8_float16"
    whisper_batch: int = 8
    # Wo die Schritte laufen: "cuda" (Grafikkarte) oder "cpu". Die Worker-App wählt das nach Hardware und der
    # eingestellten Speichergrenze (kleine Karten: Ausrichtung und Sprechertrennung auf dem Prozessor).
    whisper_device: str = "cuda"
    align_device: str = ""        # leer = wie whisper_device
    diarize_device: str = ""      # leer = wie whisper_device
    gpu_memory_limit_mb: int = 0  # 0 = keine Grenze; sonst höchstens so viel Grafikspeicher für PyTorch
    cpu_threads: int = 0          # 0 = automatisch
    # Nur für Tests: andere DB-Datei
    database_url: str | None = None

    @field_validator("data_dir")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'chronik.db'}"


STANDARD_GEHEIMNIS = "bitte-aendern"


def _geheimnis(data_dir: Path) -> str:
    """Fehlt JWT_SECRET in der .env, erzeugt der Server beim ersten Start selbst einen und merkt ihn sich im
    Datenordner (nur für den Besitzer lesbar). So muss bei der Installation niemand ein Geheimnis erzeugen."""
    import secrets

    datei = data_dir / "geheimnis.txt"
    try:
        wert = datei.read_text(encoding="utf-8").strip()
        if len(wert) >= 32:
            return wert
    except OSError:
        pass
    wert = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = datei.with_suffix(".tmp")
    tmp.write_text(wert, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(datei)
    return wert


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.jwt_secret == STANDARD_GEHEIMNIS or len(s.jwt_secret) < 32:
        s.jwt_secret = _geheimnis(s.data_dir)
    return s
