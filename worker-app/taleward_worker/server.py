"""Verbindung zum Taleward-Server: Adresse prüfen, koppeln, Fassung abfragen."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from taleward_worker import VERSION


class ServerFehler(Exception):
    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code
        self.text = text


def _klient() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20), headers={"User-Agent": f"TalewardWorker/{VERSION}"},
                        follow_redirects=False)


def adresse_normalisieren(eingabe: str) -> str:
    a = eingabe.strip().rstrip("/")
    if not a:
        raise ServerFehler("adresse_leer")
    if not re.match(r"^https?://", a, re.I):
        a = "https://" + a
    teile = urlparse(a)
    if not teile.hostname:
        raise ServerFehler("adresse_ungueltig")
    return a


def unverschluesselt(adresse: str) -> bool:
    """HTTP außerhalb dieses PCs – nur mit ausdrücklicher Erlaubnis (Heimnetz)."""
    t = urlparse(adresse)
    return t.scheme == "http" and t.hostname not in ("localhost", "127.0.0.1", "::1")


def pruefen(adresse: str) -> dict:
    """Ist dort ein Taleward-Server? Liefert Name und API-Fassung."""
    try:
        with _klient() as k:
            r = k.get(f"{adresse}/api/v1/health")
            if r.status_code in (301, 302, 307, 308) and r.headers.get("location", "").startswith("https://"):
                raise ServerFehler("https_umleitung")
            if r.status_code != 200 or r.json().get("status") != "ok":
                raise ServerFehler("kein_taleward")
            info = k.get(f"{adresse}/api/v1/info")
            daten = info.json() if info.status_code == 200 else {}
    except httpx.ConnectError:
        raise ServerFehler("nicht_erreichbar") from None
    except httpx.TimeoutException:
        raise ServerFehler("zeitueberschreitung") from None
    except (httpx.HTTPError, ValueError):
        raise ServerFehler("kein_taleward") from None
    return {"name": daten.get("serverName") or daten.get("name") or "", "apiVersion": daten.get("apiVersion", "")}


def koppeln(adresse: str, code: str, name: str) -> dict:
    code = code.strip().upper().replace(" ", "")
    if re.fullmatch(r"[A-Z0-9]{8}", code):
        code = f"{code[:4]}-{code[4:]}"
    try:
        with _klient() as k:
            r = k.post(f"{adresse}/worker/v1/pair", json={"code": code, "name": name.strip()[:60] or "worker"})
    except httpx.HTTPError:
        raise ServerFehler("nicht_erreichbar") from None
    if r.status_code != 201:
        try:
            d = r.json()
            raise ServerFehler(d.get("code", "koppeln_fehlgeschlagen"), d.get("message", ""))
        except ValueError:
            raise ServerFehler("koppeln_fehlgeschlagen", r.text[:200]) from None
    return r.json()


def konfiguration(adresse: str, token: str) -> dict:
    """Einstellungen für Worker; enthält serverVersion (ab Server 0.4.0)."""
    try:
        with _klient() as k:
            r = k.get(f"{adresse}/worker/v1/config", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        raise ServerFehler("nicht_erreichbar") from None
    if r.status_code == 401:
        raise ServerFehler("abgelehnt")
    if r.status_code != 200:
        raise ServerFehler("kein_taleward")
    return r.json()
