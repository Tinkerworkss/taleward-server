"""Server-Angaben, die die Verwaltung (Weboberfläche) ändern kann.

Vorrang: Wert in der Datenbank (Tabelle server_meta, Schlüssel „angabe.<feld>“) vor dem Wert aus der .env.
So bleibt die .env für die Ersteinrichtung und Docker nutzbar, und die Weboberfläche kann alles überschreiben.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ServerMeta

FELDER = ("server_name", "server_operator", "server_contact", "privacy_policy_url", "min_age",
          "app_min_version", "app_latest_version", "app_download_url", "app_release_notes", "public_url")
PREFIX = "angabe."


@dataclass
class Angaben:
    server_name: str
    server_operator: str
    server_contact: str | None
    privacy_policy_url: str | None
    min_age: int
    app_min_version: str | None = None
    app_latest_version: str | None = None
    app_download_url: str | None = None
    app_release_notes: str | None = None
    public_url: str | None = None  # z. B. https://taleward.meinverein.de – für Links in E-Mails und Anmeldedienste


def oeffentliche_adresse(db: Session, request=None) -> str:
    """Adresse des Servers von außen: aus den Einstellungen, sonst aus der Anfrage."""
    url = angaben(db).public_url
    if url:
        return url.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:8000"


def mail_adresse(db: Session) -> str | None:
    """Adresse für Links in E-Mails – nur die eingetragene öffentliche Adresse, nie aus der Anfrage (Host-Kopfzeile
    ist fälschbar). Ohne sie werden keine Mails mit Links verschickt."""
    url = angaben(db).public_url
    return url.rstrip("/") if url else None


def angaben(db: Session) -> Angaben:
    s = get_settings()
    werte = {f: getattr(s, f) for f in FELDER}
    for f in FELDER:
        row = db.get(ServerMeta, PREFIX + f)
        if row is not None:
            werte[f] = row.value if row.value != "" else None
    werte["min_age"] = int(werte["min_age"] or 16)
    werte["server_name"] = werte["server_name"] or s.server_name
    werte["server_operator"] = werte["server_operator"] or s.server_operator
    return Angaben(**werte)


def speichern(db: Session, **werte) -> None:
    for f, v in werte.items():
        if f not in FELDER:
            raise KeyError(f)
        row = db.get(ServerMeta, PREFIX + f)
        text = "" if v is None else str(v).strip()
        if row is None:
            db.add(ServerMeta(key=PREFIX + f, value=text))
        else:
            row.value = text


def meta_lesen(db: Session, key: str, standard: str | None = None) -> str | None:
    row = db.get(ServerMeta, key)
    return row.value if row is not None else standard


def meta_schreiben(db: Session, key: str, value: str) -> None:
    row = db.get(ServerMeta, key)
    if row is None:
        db.add(ServerMeta(key=key, value=value))
    else:
        row.value = value


# ---------------------------------------------------------------- Versionen (0.3.9)
def version_tupel(v: str | None) -> tuple[int, ...] | None:
    """„0.10.0“ → (0, 10, 0). Numerisch je Stelle, damit 0.10.0 > 0.9.9. Ungültig → None."""
    if not v:
        return None
    try:
        return tuple(int(x) for x in v.strip().lstrip("vV").split("."))
    except ValueError:
        return None


_min_cache: dict = {"wert": None, "zeit": 0.0}


def mindestversion(db_factory) -> str | None:
    """Mindestversion der App, 30 s zwischengespeichert (wird bei jeder API-Anfrage gebraucht)."""
    import time

    if time.monotonic() - _min_cache["zeit"] > 30:
        with db_factory() as db:
            _min_cache["wert"] = angaben(db).app_min_version
        _min_cache["zeit"] = time.monotonic()
    return _min_cache["wert"]


def mindestversion_vergessen() -> None:
    _min_cache["zeit"] = 0.0


# ---------------------------------------------------------------- Externe Transkription
@dataclass
class ExternKonfig:
    anbieter: str | None  # "mistral" nur, wenn freigegeben UND Schlüssel vorhanden
    gewaehlt: str  # was eingestellt ist ("" = aus)
    api_key: str | None
    stunden: float
    modell: str
    url: str
    max_sekunden: float
    cent_pro_minute: float
    quelle: str  # "verwaltung" | "env"


def extern_konfig(db: Session) -> ExternKonfig:
    """Werte aus der Verwaltung (server_meta „extern.*“) vor denen aus der .env."""
    s = get_settings()
    gesetzt = db.get(ServerMeta, "extern.anbieter") is not None
    gewaehlt = (meta_lesen(db, "extern.anbieter") if gesetzt else s.external_transcription) or ""
    key = meta_lesen(db, "extern.api_key") if db.get(ServerMeta, "extern.api_key") else s.mistral_api_key
    stunden_txt = meta_lesen(db, "extern.after_hours")
    stunden = float(stunden_txt) if stunden_txt else s.external_after_hours
    return ExternKonfig(anbieter="mistral" if gewaehlt == "mistral" and key else None, gewaehlt=gewaehlt,
                        api_key=key or None, stunden=stunden, modell=s.external_model, url=s.external_api_url,
                        max_sekunden=s.external_max_seconds, cent_pro_minute=s.external_cost_cents_per_minute,
                        quelle="verwaltung" if gesetzt else "env")


# ---------------------------------------------------------------- Sprachmodell
LLM_ARTEN = ("aus", "attrappe", "lokal", "api")


@dataclass
class LlmKonfig:
    art: str  # aus | attrappe | lokal | api
    api_url: str
    api_modell: str
    api_key: str | None  # eigener Schlüssel oder – bei Mistral – der der externen Transkription
    eigener_key: bool
    lokal_modell: str
    lokal_kontext: int
    cent_ein: float | None  # Preis je 1 Mio. Tokens; None = aus der Preistabelle
    cent_aus: float | None

    @property
    def ist_mistral(self) -> bool:
        return "api.mistral.ai" in self.api_url

    @property
    def bereit(self) -> bool:
        """Kann die Zentrale selbst zusammenfassen (Attrappe oder API mit Schlüssel)?"""
        return self.art == "attrappe" or (self.art == "api" and bool(self.api_key))


def llm_konfig(db: Session) -> LlmKonfig:
    """Werte aus der Verwaltung (server_meta „llm.*“) vor denen aus der .env."""
    s = get_settings()

    def wert(schluessel: str, standard):
        return meta_lesen(db, schluessel) if db.get(ServerMeta, schluessel) is not None else standard

    art = wert("llm.art", s.summarizer) or "aus"
    if art not in LLM_ARTEN:
        art = "aus"
    url = wert("llm.api_url", s.llm_api_url) or s.llm_api_url
    eigener = wert("llm.api_key", s.llm_api_key) or None
    key = eigener
    if not key and "api.mistral.ai" in url:
        key = extern_konfig(db).api_key
    try:
        kontext = int(wert("llm.lokal_kontext", s.llm_local_context))
    except (TypeError, ValueError):
        kontext = s.llm_local_context

    def preis(schluessel):
        try:
            v = wert(schluessel, None)
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    return LlmKonfig(art=art, api_url=url, api_modell=wert("llm.api_modell", s.llm_api_model) or s.llm_api_model,
                     api_key=key, eigener_key=bool(eigener),
                     lokal_modell=wert("llm.lokal_modell", s.llm_local_model) or s.llm_local_model,
                     lokal_kontext=kontext, cent_ein=preis("llm.cent_ein"), cent_aus=preis("llm.cent_aus"))
