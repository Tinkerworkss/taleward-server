"""Welches Sprachmodell ein Worker für Recaps nimmt und wie groß der Kontext wird.

„auto“ (Standard): nach dem Grafikspeicher, den der Worker nutzen darf – gemessen im Modellvergleich vom 01.10.2026
(docs/RECAP-MODELLE.md). Der Kontext richtet sich nach der Länge des Transkripts: Passt es ganz hinein, schreibt das
Modell den Recap aus dem Transkript selbst statt aus verdichteten Notizen – das war der größte Qualitätsunterschied.
"""
from __future__ import annotations

AUTO = "auto"
KLEIN = "gemma4:e4b"
GROSS = "gemma4:12b"
MIN_KONTEXT = 12288
RESERVE = 5000   # Anweisung, Kopf, Bibel-Namen und Antwort
RASTER = 2048    # Kontext in Stufen – Ollama lädt das Modell nur neu, wenn sich der Kontext ändert
OHNE_KARTE_KONTEXT = 20480  # Prozessor oder Karte, die wir nicht messen können (AMD, Apple)

# ab MB nutzbarem Grafikspeicher: Modell, höchster Kontext
STUFEN = [
    (15000, GROSS, 32768),  # 16-GB-Karte: 12b ganz auf der Karte, ~80 s für 47 min Spiel
    (12000, GROSS, 24576),  # 12-GB-Karte: etwas weniger Kontext, damit nichts in den Arbeitsspeicher ausweicht
    (6000, KLEIN, 20480),   # 8-GB-Karte: e4b ganz auf der Karte, ~1 min; 12b wiche hier aus (4 min)
    (0, KLEIN, MIN_KONTEXT),
]
# Modelle, die eine neuere Ollama-Fassung brauchen
MIN_OLLAMA = {"gemma4": (0, 35, 0)}


def budget_mb(vram_mb: int | None, grenze_mb: int | None) -> int | None:
    """Nutzbarer Grafikspeicher: die Karte, höchstens die Grenze aus der Worker-App (0 = keine Grenze)."""
    if not vram_mb:
        return None
    return min(vram_mb, grenze_mb) if grenze_mb else vram_mb


def wahl(vram_mb: int | None, grenze_mb: int | None = 0) -> tuple[str, int]:
    """(Modell, höchster Kontext) für diesen Worker."""
    b = budget_mb(vram_mb, grenze_mb)
    if b is None:
        return KLEIN, OHNE_KARTE_KONTEXT
    return next((m, k) for ab, m, k in STUFEN if b >= ab)


def kontext_fuer(bedarf_tokens: int, hoechstens: int) -> int:
    """Kontext für einen Auftrag: Bedarf + Reserve, in Stufen gerundet, zwischen MIN_KONTEXT und hoechstens.
    Ist hoechstens kleiner als MIN_KONTEXT (von Hand so eingestellt), gilt hoechstens."""
    soll = -(-(bedarf_tokens + RESERVE) // RASTER) * RASTER
    return min(hoechstens, max(MIN_KONTEXT, soll))


def bedarf(recap_ein: dict) -> int:
    """Geschätzte Tokens der Recap-Eingabe: Transkript plus Bibel (Namen, Zusammenfassungen gekürzt)."""
    from app.sprachmodell import tokens, transkript_zeilen

    text = "\n".join(transkript_zeilen(recap_ein.get("transkript") or []))
    bibel = sum(len(str(e.get("name") or "")) + min(500, len(str(e.get("zusammenfassung") or "")))
                for e in recap_ein.get("bibel") or [])
    return tokens(text) + int(bibel / 3.2)


def aufloesen(modell: str, kontext: int, auto: bool = False) -> tuple[str, int]:
    """Modell und höchsten Kontext für diesen PC: bei „auto“ nach Grafikkarte, sonst wie eingestellt."""
    if auto or modell == AUTO:
        from app.arbeitsweise import grafikspeicher_mb
        from app.config import get_settings

        return wahl(grafikspeicher_mb(), get_settings().gpu_memory_limit_mb)
    return modell, kontext


def _fassung(text: str | None) -> tuple[int, ...]:
    teile = []
    for t in (text or "").split("-")[0].split(".")[:3]:
        if not t.isdigit():
            break
        teile.append(int(t))
    return tuple(teile)


def ollama_zu_alt(modell: str, version: str | None) -> str | None:
    """Mindestfassung, falls Ollama für dieses Modell zu alt ist (sonst None)."""
    v = _fassung(version)
    for vorsilbe, mindestens in MIN_OLLAMA.items():
        if modell.startswith(vorsilbe) and v and v < mindestens:
            return ".".join(map(str, mindestens))
    return None
