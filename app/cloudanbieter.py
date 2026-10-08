"""Bekannte Cloud-Anbieter für die Zusammenfassung (OpenAI-kompatible Schnittstelle).

Die Tabelle ist eine Hilfe für die Verwaltung: Adresse, empfohlenes Modell, Standort der Verarbeitung, was der
Anbieter zum Training mit Kundendaten sagt, und wie man an einen Schlüssel kommt. Die Angaben stammen von den
Seiten der Anbieter (Datum in STAND) und sind ohne Gewähr – Betreiber prüfen sie selbst und schließen den Vertrag zur
Auftragsverarbeitung selbst ab. Wer hier nicht steht, lässt sich als „Anderer“ mit eigener Adresse eintragen.

Texte sind Deutsch; die englischen Fassungen stehen in app/verwaltung/i18n.py (Test: jeder Text ist übersetzt).
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

STAND = "2026-10-08"  # Datum, an dem die Angaben zuletzt geprüft wurden – in der Verwaltung sichtbar


@dataclass(frozen=True)
class Anbieter:
    id: str
    name: str
    url: str  # OpenAI-kompatible Basisadresse (…/v1)
    modell: str  # empfohlenes Modell
    eu: bool  # Verarbeitung in der EU
    sitz: str  # wo verarbeitet wird (Text für die Verwaltung)
    training: str  # was der Anbieter zum Training mit Kundendaten sagt
    anmeldung: str  # Klickpfad zum Schlüssel
    hinweis: str = ""  # Besonderheiten (Schnittstelle, Modelle)
    hilfe: str = ""  # Adresse der Anbieter-Dokumentation
    anzeige: str = ""  # Name für die App ohne Sprachbezug (Schnittstelle 0.4.11, CloudProviderInfo.name)
    land: str | None = None  # Land der Verarbeitung, ISO-3166-1-alpha-2 (CloudProviderInfo.country)

    def info(self) -> dict:
        """CloudProviderInfo für ServerInfo (0.4.11): id, name, region, country."""
        return {"id": self.id, "name": self.anzeige or self.name, "region": "eu" if self.eu else "non_eu",
                "country": self.land}


ANBIETER: tuple[Anbieter, ...] = (
    Anbieter("mistral", "Mistral (EU)", "https://api.mistral.ai/v1", "mistral-large-latest", True,
             "Frankreich / EU",
             "Im kostenlosen Tarif („Experiment“) darf Mistral Ein- und Ausgaben zum Training nutzen; im bezahlten Tarif "
             "lässt es sich abschalten. Abschalten: admin.mistral.ai → Privacy → „Anonymous improvement data“ aus. Der "
             "Schalter für Vibe ist ein eigener und zählt hier nicht.",
             "console.mistral.ai → Konto anlegen → Workspace → „API Keys“ (nicht unter „Code“: Vibe-Schlüssel hängen am "
             "Vibe-Budget) → „Create new key“ → Schlüssel kopieren. Dann unter „Billing“ die Abrechnung „Pay as you go“ "
             "einschalten – Guthaben aufladen allein reicht nicht. Im kostenlosen Tarif sind die Grenzen sehr niedrig "
             "(oft Fehler 429), und nicht jedes Modell ist freigeschaltet.",
             "Preise der Mistral-Modelle sind hinterlegt; der Schlüssel der externen Transkription gilt auch hier.",
             "https://docs.mistral.ai", anzeige="Mistral AI", land="FR"),
    Anbieter("ionos", "IONOS AI Model Hub (DE)", "https://openai.inference.de-txl.ionos.com/v1",
             "openai/gpt-oss-120b", True, "Deutschland (Berlin)",
             "Laut IONOS werden Kundendaten nie zum Training genutzt und nicht gespeichert; Vertrag zur "
             "Auftragsverarbeitung ist Teil des Vertrags.",
             "cloud.ionos.de → Konto anlegen → im Data Center Designer ein Zugriffs-Token erzeugen → Token kopieren. Den genauen "
             "Weg beschreibt die IONOS-Dokumentation (Link unten).",
             "Offene Modelle (gpt-oss, Llama, Qwen). Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://docs.ionos.com/cloud/ai/ai-model-hub", anzeige="IONOS AI Model Hub", land="DE"),
    Anbieter("stackit", "STACKIT AI Model Serving (DE)",
             "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1", "openai/gpt-oss-120b", True,
             "Deutschland (Neckarsulm)",
             "Laut STACKIT kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung über das Kundenkonto.",
             "portal.stackit.cloud → Konto und Projekt anlegen → „AI Model Serving“ → Token erzeugen → Token kopieren. Den genauen "
             "Weg beschreibt die STACKIT-Dokumentation (Link unten).",
             "Offene Modelle (gpt-oss, Llama, Qwen, Gemma). Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://docs.stackit.cloud", anzeige="STACKIT AI Model Serving", land="DE"),
    Anbieter("scaleway", "Scaleway Generative APIs (FR)", "https://api.scaleway.ai/v1", "gpt-oss-120b", True,
             "Frankreich (Paris)",
             "Laut Scaleway kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung in den AGB.",
             "console.scaleway.com → Konto anlegen → „IAM“ → „API keys“ → „Generate API key“ → Secret key kopieren.",
             "Offene Modelle. Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://www.scaleway.com/en/docs/generative-apis", anzeige="Scaleway", land="FR"),
    Anbieter("ovh", "OVHcloud AI Endpoints (FR)", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
             "Meta-Llama-3_3-70B-Instruct", True, "Frankreich",
             "Laut OVHcloud kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung in den AGB.",
             "ovhcloud.com → Kundenkonto → „Public Cloud“ → „AI Endpoints“ → Schlüssel erzeugen und kopieren. Den genauen Weg "
             "beschreibt die OVHcloud-Dokumentation (Link unten).",
             "Offene Modelle; Modellnamen sind hier groß-/kleinschreibungsempfindlich. Die genauen Namen zeigt "
             "„Verbindung prüfen“.",
             "https://endpoints.ai.cloud.ovh.net", anzeige="OVHcloud", land="FR"),
    Anbieter("openai", "OpenAI (US)", "https://api.openai.com/v1", "gpt-5-mini", False,
             "USA (EU-Datenresidenz gegen Aufpreis wählbar)",
             "Laut OpenAI werden Daten aus der API standardmäßig nicht zum Training genutzt.",
             "platform.openai.com → Konto anlegen → „Billing“ Guthaben aufladen → „API keys“ → „Create new secret key“ "
             "→ Schlüssel kopieren.",
             "", "https://platform.openai.com/docs", anzeige="OpenAI", land="US"),
    Anbieter("google", "Google Gemini (US)", "https://generativelanguage.googleapis.com/v1beta/openai",
             "gemini-flash-latest", False, "USA",
             "Im kostenlosen Tarif nutzt Google die Inhalte zur Verbesserung seiner Produkte, im bezahlten Tarif laut "
             "Google nicht. Für Taleward also nur mit eingerichteter Abrechnung.",
             "aistudio.google.com → Konto → „Get API key“ → Projekt mit Abrechnung wählen oder anlegen → Schlüssel "
             "kopieren.",
             "OpenAI-kompatible Schnittstelle von Google ist als Beta gekennzeichnet.",
             "https://ai.google.dev/gemini-api/docs/openai", anzeige="Google Gemini", land="US"),
    Anbieter("anthropic", "Anthropic Claude (US)", "https://api.anthropic.com/v1", "claude-sonnet-4-6", False,
             "USA",
             "Laut Anthropic werden Daten aus der API standardmäßig nicht zum Training genutzt.",
             "platform.claude.com → Konto anlegen → Guthaben aufladen → „API Keys“ → „Create Key“ → Schlüssel kopieren. "
             "Ein Claude-Abo (Pro/Max) lässt sich nicht nutzen; Anthropic untersagt das für fremde Programme.",
             "Anthropic bezeichnet seine OpenAI-kompatible Schnittstelle als Testzugang; sie ignoriert den JSON-Modus. "
             "Taleward fängt das ab, es kann aber häufiger zu Wiederholungen kommen.",
             "https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk",
             anzeige="Anthropic Claude", land="US"),
    Anbieter("deepseek", "DeepSeek (CN)", "https://api.deepseek.com/v1", "deepseek-flash", False,
             "China",
             "Daten werden laut DeepSeek auf Servern in China verarbeitet; Training lässt sich nur in den "
             "Kontoeinstellungen abwählen. Mehrere EU-Datenschutzbehörden prüfen den Anbieter.",
             "platform.deepseek.com → Konto anlegen → Guthaben aufladen → „API keys“ → „Create new API key“ → Schlüssel "
             "kopieren.",
             "", "https://api-docs.deepseek.com", anzeige="DeepSeek", land="CN"),
)

_NACH_ID = {a.id: a for a in ANBIETER}


def finden(anbieter_id: str | None) -> Anbieter | None:
    return _NACH_ID.get(anbieter_id or "")


def erkennen(url: str | None) -> Anbieter | None:
    """Anbieter zur eingestellten Adresse – über den Host, damit auch ältere Einstellungen zugeordnet werden."""
    host = urlsplit(url or "").hostname or ""
    for a in ANBIETER:
        if host and host == urlsplit(a.url).hostname:
            return a
    return None


def texte() -> set[str]:
    """Alle Texte, die in der Verwaltung angezeigt werden (für die Übersetzungsprüfung)."""
    aus: set[str] = set()
    for a in ANBIETER:
        aus |= {a.sitz, a.training, a.anmeldung}
        if a.hinweis:
            aus.add(a.hinweis)
    return aus


def info_fuer(kennung: str | None, url: str | None = None) -> dict | None:
    """CloudProviderInfo zu einer Kennung bzw. Adresse. Unbekannter Anbieter: Host als Name, vorsichtig non_eu,
    Land unbekannt. None ohne Anbieter."""
    if not kennung:
        return None
    a = finden(kennung) or erkennen(url)
    if a:
        return a.info()
    return {"id": kennung, "name": urlsplit(url or "").hostname or kennung, "region": "non_eu", "country": None}
