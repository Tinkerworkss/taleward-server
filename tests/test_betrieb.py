"""Laufender Betrieb: Benachrichtigungen an den Betreiber, QR-Code, Passwort-Link."""
import re
from datetime import timedelta

import pytest

from tests.test_verwaltung import admin, csrf_von  # noqa: F401 (Fixture)


@pytest.fixture()
def ntfy(monkeypatch):
    """Fängt alle ntfy-Nachrichten ab (kein Netz)."""
    import httpx

    from app import benachrichtigung

    gesendet = []

    class Antwort:
        status_code = 200

    def post(url, json=None, headers=None, timeout=None):
        gesendet.append({"url": url, **json, "headers": headers or {}})
        return Antwort()

    monkeypatch.setattr(benachrichtigung.httpx, "post", post)
    assert httpx  # Modul bleibt importiert
    return gesendet


def einrichten(dbs, **werte):
    from app.einstellungen import meta_schreiben

    for k, v in {"ntfy_url": "https://ntfy.example.org/verein-taleward-7k2q", **werte}.items():
        meta_schreiben(dbs, f"melden.{k}", v)
    dbs.commit()


def test_einstellungen_und_test(client, dbs, admin, ntfy):  # noqa: F811
    seite = client.get("/verwaltung/einstellungen")
    assert 'id="benachrichtigungen"' in seite.text
    r = client.post("/verwaltung/benachrichtigungen", data={"csrf": admin, "ntfy_url": "ntfy.sh/ohne-schema"})
    assert r.status_code == 400
    r = client.post("/verwaltung/benachrichtigungen", data={
        "csrf": admin, "ntfy_url": "https://ntfy.example.org/verein-taleward-7k2q", "ntfy_token": "tk_geheim123",
        "smtp_host": "", "stunden": "4", "sprache": "de", "aktion": "test"}, follow_redirects=False)
    assert r.status_code == 303 and "melden_test" in r.headers["location"]
    assert ntfy[-1]["url"] == "https://ntfy.example.org/" and ntfy[-1]["topic"] == "verein-taleward-7k2q"
    assert ntfy[-1]["headers"]["Authorization"] == "Bearer tk_geheim123"
    assert ntfy[-1]["title"].startswith("Taleward · ")
    seite = client.get("/verwaltung/einstellungen").text
    assert "tk_geheim123" not in seite and "leer lassen" in seite  # nur schreibbar
    # Mailserver ohne Empfänger ist erlaubt (dient dann nur „Passwort vergessen“); ungültiger Empfänger nicht
    r = client.post("/verwaltung/benachrichtigungen", data={"csrf": admin, "smtp_host": "smtp.example.org",
                                                            "email_an": "kein-at"})
    assert r.status_code == 400
    # Token löschen
    client.post("/verwaltung/benachrichtigungen", data={"csrf": admin, "zugang_loeschen": "1",
                                                        "ntfy_url": "https://ntfy.example.org/x"})
    from app.einstellungen import meta_lesen

    dbs.expire_all()
    assert not meta_lesen(dbs, "melden.ntfy_token")


def test_ohne_einrichtung_keine_meldung(client, dbs, ntfy):
    from app import benachrichtigung

    assert benachrichtigung.pruefen(dbs) == [] and ntfy == []


def test_worker_fehlt_und_wiederholung(client, dbs, ntfy):
    from app import benachrichtigung
    from app.db import utcnow
    from app.einstellungen import meta_schreiben
    from app.models import Job

    einrichten(dbs, stunden="6")
    dbs.add(Job(type="transcribe", required_capability="asr", created_at=utcnow() - timedelta(hours=2)))
    dbs.commit()
    assert "worker_fehlt" not in benachrichtigung.pruefen(dbs)  # noch nicht lange genug
    dbs.add(Job(type="transcribe", required_capability="asr", created_at=utcnow() - timedelta(hours=7)))
    dbs.commit()
    assert "worker_fehlt" in benachrichtigung.pruefen(dbs)
    assert "1 Auftrag" in ntfy[-1]["message"] and "6 Stunden" in ntfy[-1]["message"]
    assert "worker_fehlt" not in benachrichtigung.pruefen(dbs)  # höchstens alle 24 Stunden
    # In der Betriebsart „Nur Cloud“ kein Hinweis
    meta_schreiben(dbs, "betrieb.art", "cloud")
    meta_schreiben(dbs, "melden.zuletzt.worker_fehlt", "")
    dbs.commit()
    assert "worker_fehlt" not in benachrichtigung.pruefen(dbs)


def test_fehlschlag_ohne_inhalte(client, dbs, world, ntfy):
    from app import benachrichtigung
    from app.db import utcnow
    from app.models import Job

    einrichten(dbs)
    assert benachrichtigung.pruefen(dbs) == []  # erster Lauf merkt nur den Stand
    dbs.add(Job(type="summarize", required_capability="llm", state="failed", finished_at=utcnow() + timedelta(seconds=1),
                error_code="llm_invalid", error_message="Rabenfels: Kapitel über den Fährmann"))
    dbs.commit()
    assert "fehlschlag" in benachrichtigung.pruefen(dbs)
    text = ntfy[-1]["message"]
    assert "Zusammenfassung (llm_invalid)" in text
    assert "Rabenfels" not in text and "Fährmann" not in text


def test_kostenlimit_80_und_100(client, dbs, ntfy):
    from app import benachrichtigung
    from app.einstellungen import meta_schreiben
    from app.models import UsageLog

    einrichten(dbs, sprache="en")
    meta_schreiben(dbs, "kosten.limit_cent", "1000")
    dbs.add(UsageLog(campaign_id="c", kind="transcription", engine="external", cost_cents=850))
    dbs.commit()
    assert "kosten_80" in benachrichtigung.pruefen(dbs)
    assert "€8.50 of €10.00" in ntfy[-1]["message"]  # Sprache der Meldungen: Englisch
    assert benachrichtigung.pruefen(dbs) == []  # einmal pro Monat
    dbs.add(UsageLog(campaign_id="c", kind="summary", engine="external", cost_cents=200))
    dbs.commit()
    assert benachrichtigung.pruefen(dbs) == ["kosten_100"]


def test_sicherung_und_speicher(client, dbs, ntfy, monkeypatch):
    from collections import namedtuple

    from app import benachrichtigung

    einrichten(dbs)
    benachrichtigung.sicherung_fehlgeschlagen(dbs, "OSError: Platte voll")
    assert "sicherung" in benachrichtigung.pruefen(dbs)
    assert "Platte voll" in ntfy[-1]["message"]
    benachrichtigung.sicherung_gelungen(dbs)
    assert "sicherung" not in benachrichtigung.pruefen(dbs)

    Nutzung = namedtuple("Nutzung", "total used free")
    monkeypatch.setattr(benachrichtigung.shutil, "disk_usage", lambda p: Nutzung(100, 99, 500 * 1024 ** 2))
    assert "speicher" in benachrichtigung.pruefen(dbs)
    assert "0.5 GB" in ntfy[-1]["message"]


def test_versandfehler_wird_angezeigt(client, dbs, admin, monkeypatch):  # noqa: F811
    from app import benachrichtigung

    class Antwort:
        status_code = 403

    monkeypatch.setattr(benachrichtigung.httpx, "post", lambda *a, **k: Antwort())
    r = client.post("/verwaltung/benachrichtigungen", data={
        "csrf": admin, "ntfy_url": "https://ntfy.example.org/t", "aktion": "test"})
    assert r.status_code == 502 and "403" in r.text


# ---------------------------------------------------------------- Passwort-Link
def test_passwort_link(client, dbs, admin, login):  # noqa: F811
    from sqlalchemy import select

    from app.models import User

    mitglied = dbs.scalar(select(User).where(User.username == "mitglied"))
    alt = login("mitglied", "geheim123")
    r = client.post(f"/verwaltung/konten/{mitglied.id}/link", data={"csrf": admin})
    assert r.status_code == 200 and 'src="data:image/svg+xml' in r.text
    link = re.search(r'value="(http://testserver/passwort/[^"]+)"', r.text).group(1)
    pfad = link.removeprefix("http://testserver")
    seite = client.get(pfad)
    assert seite.status_code == 200 and "„mitglied“" in seite.text
    assert client.get(pfad + "x").status_code == 404  # falsches Geheimnis
    r = client.post(pfad, data={"password": "kurz", "password2": "kurz"})
    assert r.status_code == 400
    r = client.post(pfad, data={"password": "Laternenmann9", "password2": "Laternenmann8"})
    assert r.status_code == 400 and "stimmen nicht" in r.text
    r = client.post(pfad, data={"password": "Laternenmann9", "password2": "Laternenmann9"})
    assert r.status_code == 200 and "Passwort gespeichert" in r.text
    assert client.get("/api/v1/me", headers=alt).status_code == 401  # überall abgemeldet
    assert login("mitglied", "Laternenmann9")
    assert client.get(pfad).status_code == 404  # nur einmal
    # Ein neuer Link ersetzt den alten; abgelaufene Links gelten nicht
    r1 = client.post(f"/verwaltung/konten/{mitglied.id}/link", data={"csrf": admin}).text
    r2 = client.post(f"/verwaltung/konten/{mitglied.id}/link", data={"csrf": admin}).text
    p1 = re.search(r"/passwort/[^\"]+", r1).group(0)
    p2 = re.search(r"/passwort/[^\"]+", r2).group(0)
    assert client.get(p1).status_code == 404 and client.get(p2).status_code == 200
    from app.einstellungen import meta_lesen, meta_schreiben

    dbs.expire_all()
    wert = meta_lesen(dbs, f"pwlink.{mitglied.id}")
    meta_schreiben(dbs, f"pwlink.{mitglied.id}", wert.split("|")[0] + "|2020-01-01T00:00:00+00:00")
    dbs.commit()
    assert client.get(p2).status_code == 404
    assert client.get(p2).headers["Referrer-Policy"] == "no-referrer"


# ---------------------------------------------------------------- Auftragsverarbeitung und Übergabe
def test_uebergabe_seite(client, dbs, admin):  # noqa: F811
    from app.verwaltung.i18n import EN
    from app.verwaltung.uebergabe import ANBIETER

    for a in ANBIETER.values():
        assert a["name"] in EN and a["hilfe"] in EN
    seite = client.get("/verwaltung/uebergabe")
    assert seite.status_code == 200 and "Verwaltung übergeben" in seite.text and "Chefin (chef)" in seite.text
    r = client.post("/verwaltung/uebergabe/av", data={"csrf": admin, "av_hosting": "2026-09-20", "av_mistral": "kaputt"},
                    follow_redirects=False)
    assert r.status_code == 303
    seite = client.get("/verwaltung/uebergabe").text
    assert 'value="2026-09-20"' in seite
    client.cookies.set("tw_sprache", "en")
    en = client.get("/verwaltung/uebergabe").text
    assert "Hand over the admin role" in en and "Hugging Face access" in en
