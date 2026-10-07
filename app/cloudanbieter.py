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

STAND = "2026-10-07"  # Datum, an dem die Angaben zuletzt geprüft wurden – in der Verwaltung sichtbar


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


ANBIETER: tuple[Anbieter, ...] = (
    Anbieter("mistral", "Mistral (EU)", "https://api.mistral.ai/v1", "mistral-large-latest", True,
             "Frankreich / EU",
             "Im kostenlosen Tarif („Experiment“) darf Mistral Ein- und Ausgaben zum Training nutzen; im bezahlten Tarif "
             "lässt es sich abschalten. Abschalten: admin.mistral.ai → Privacy → „Anonymous improvement data“ aus. Der "
             "Schalter für Vibe ist ein eigener und zählt hier nicht.",
             "console.mistral.ai → Konto anlegen → Workspace → „API Keys“ → „Create new key“ → Schlüssel kopieren. Für "
             "den bezahlten Tarif unter „Billing“ eine Zahlungsart hinterlegen.",
             "Preise der Mistral-Modelle sind hinterlegt; der Schlüssel der externen Transkription gilt auch hier.",
             "https://docs.mistral.ai"),
    Anbieter("ionos", "IONOS AI Model Hub (DE)", "https://openai.inference.de-txl.ionos.com/v1",
             "openai/gpt-oss-120b", True, "Deutschland (Berlin)",
             "Laut IONOS werden Kundendaten nie zum Training genutzt und nicht gespeichert; Vertrag zur "
             "Auftragsverarbeitung ist Teil des Vertrags.",
             "cloud.ionos.de → Konto anlegen → im Data Center Designer ein Zugriffs-Token erzeugen → Token kopieren. Den genauen "
             "Weg beschreibt die IONOS-Dokumentation (Link unten).",
             "Offene Modelle (gpt-oss, Llama, Qwen). Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://docs.ionos.com/cloud/ai/ai-model-hub"),
    Anbieter("stackit", "STACKIT AI Model Serving (DE)",
             "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1", "openai/gpt-oss-120b", True,
             "Deutschland (Neckarsulm)",
             "Laut STACKIT kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung über das Kundenkonto.",
             "portal.stackit.cloud → Konto und Projekt anlegen → „AI Model Serving“ → Token erzeugen → Token kopieren. Den genauen "
             "Weg beschreibt die STACKIT-Dokumentation (Link unten).",
             "Offene Modelle (gpt-oss, Llama, Qwen, Gemma). Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://docs.stackit.cloud"),
    Anbieter("scaleway", "Scaleway Generative APIs (FR)", "https://api.scaleway.ai/v1", "gpt-oss-120b", True,
             "Frankreich (Paris)",
             "Laut Scaleway kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung in den AGB.",
             "console.scaleway.com → Konto anlegen → „IAM“ → „API keys“ → „Generate API key“ → Secret key kopieren.",
             "Offene Modelle. Die genauen Modellnamen zeigt „Verbindung prüfen“.",
             "https://www.scaleway.com/en/docs/generative-apis"),
    Anbieter("ovh", "OVHcloud AI Endpoints (FR)", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
             "Meta-Llama-3_3-70B-Instruct", True, "Frankreich",
             "Laut OVHcloud kein Training mit Kundendaten; Vertrag zur Auftragsverarbeitung in den AGB.",
             "ovhcloud.com → Kundenkonto → „Public Cloud“ → „AI Endpoints“ → Schlüssel erzeugen und kopieren. Den genauen Weg "
             "beschreibt die OVHcloud-Dokumentation (Link unten).",
             "Offene Modelle; Modellnamen sind hier groß-/kleinschreibungsempfindlich. Die genauen Namen zeigt "
             "„Verbindung prüfen“.",
             "https://endpoints.ai.cloud.ovh.net"),
    Anbieter("openai", "OpenAI (US)", "https://api.openai.com/v1", "gpt-5-mini", False,
             "USA (EU-Datenresidenz gegen Aufpreis wählbar)",
             "Laut OpenAI werden Daten aus der API standardmäßig nicht zum Training genutzt.",
             "platform.openai.com → Konto anlegen → „Billing“ Guthaben aufladen → „API keys“ → „Create new secret key“ "
             "→ Schlüssel kopieren.",
             "", "https://platform.openai.com/docs"),
    Anbieter("google", "Google Gemini (US)", "https://generativelanguage.googleapis.com/v1beta/openai",
             "gemini-flash-latest", False, "USA",
             "Im kostenlosen Tarif nutzt Google die Inhalte zur Verbesserung seiner Produkte, im bezahlten Tarif laut "
             "Google nicht. Für Taleward also nur mit eingerichteter Abrechnung.",
             "aistudio.google.com → Konto → „Get API key“ → Projekt mit Abrechnung wählen oder anlegen → Schlüssel "
             "kopieren.",
             "OpenAI-kompatible Schnittstelle von Google ist als Beta gekennzeichnet.",
             "https://ai.google.dev/gemini-api/docs/openai"),
    Anbieter("anthropic", "Anthropic Claude (US)", "https://api.anthropic.com/v1", "claude-sonnet-4-6", False,
             "USA",
             "Laut Anthropic werden Daten aus der API standardmäßig nicht zum Training genutzt.",
             "platform.claude.com → Konto anlegen → Guthaben aufladen → „API Keys“ → „Create Key“ → Schlüssel kopieren. "
             "Ein Claude-Abo (Pro/Max) lässt sich nicht nutzen; Anthropic untersagt das für fremde Programme.",
             "Anthropic bezeichnet seine OpenAI-kompatible Schnittstelle als Testzugang; sie ignoriert den JSON-Modus. "
             "Taleward fängt das ab, es kann aber häufiger zu Wiederholungen kommen.",
             "https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk"),
    Anbieter("deepseek", "DeepSeek (CN)", "https://api.deepseek.com/v1", "deepseek-flash", False,
             "China",
             "Daten werden laut DeepSeek auf Servern in China verarbeitet; Training lässt sich nur in den "
             "Kontoeinstellungen abwählen. Mehrere EU-Datenschutzbehörden prüfen den Anbieter.",
             "platform.deepseek.com → Konto anlegen → Guthaben aufladen → „API keys“ → „Create new API key“ → Schlüssel "
             "kopieren.",
             "", "https://api-docs.deepseek.com"),
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
