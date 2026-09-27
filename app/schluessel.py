"""Schlüssel gleich beim Eintragen prüfen (Einrichtungsassistent und Verwaltung).

Ergebnis: "ok", "falsch" (der Anbieter lehnt ab), "kein_zugang" (Hugging Face: Modellbedingungen nicht
angenommen) oder "unbekannt" (Anbieter nicht erreichbar – der Schlüssel wird trotzdem gespeichert).
"""
from __future__ import annotations

import httpx

from app.modelle import SPRECHERMODELL

_client: httpx.Client | None = None  # in Tests ersetzt


def _c() -> httpx.Client:
    return _client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))


def mistral(key: str) -> str:
    try:
        r = _c().get("https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return "unbekannt"
    if r.status_code == 200:
        return "ok"
    return "falsch" if r.status_code in (401, 403) else "unbekannt"


def huggingface(token: str) -> str:
    try:
        r = _c().get("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (401, 403):
            return "falsch"
        if r.status_code != 200:
            return "unbekannt"
        z = _c().head(f"https://huggingface.co/{SPRECHERMODELL}/resolve/main/config.yaml",
                      headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)
    except httpx.HTTPError:
        return "unbekannt"
    if z.status_code in (200, 302, 307):
        return "ok"
    return "kein_zugang" if z.status_code in (401, 403) else "unbekannt"
