"""Weboberfläche, Teil 2: Sprachen, Transkription-Seite mit externer Transkription, Einladungsseite, App-Versionen."""
import pathlib
import re
from urllib.parse import quote

import pytest

from tests.test_verwaltung import admin, csrf_von  # noqa: F401 (Fixture)

API = "/api/v1"


# ---------------------------------------------------------------- Übersetzung
def ui_texte() -> set[str]:
    texte = set()
    for f in pathlib.Path("app/verwaltung/templates").glob("*.html"):
        s = f.read_text()
        texte |= set(re.findall(r'_\(\s*"((?:[^"\\]|\\.)*)"', s)) | set(re.findall(r"_\(\s*'((?:[^'\\]|\\.)*)'", s))
    r = pathlib.Path("app/verwaltung/router.py").read_text()
    texte |= set(re.findall(r'(?:\b_|tr\(request\))\(\s*"((?:[^"\\]|\\.)*)"', r))
    a = pathlib.Path("app/verwaltung/assistent.py").read_text()
    texte |= set(re.findall(r'(?:\b_|tr\(request\))\(\s*"((?:[^"\\]|\\.)*)"', a))
    schritte = re.search(r"SCHRITTE = \[(.*?)\]", a, re.S).group(1)
    texte |= set(re.findall(r'\("\w+", "([^"]+)"\)', schritte))
    e = pathlib.Path("app/einrichtung.py").read_text()
    texte |= set(re.findall(r'Punkt\("\w+", "([^"]+)"', e))
    m = re.search(r"MELDUNGEN = \{(.*?)\n\}", r, re.S)
    texte |= set(re.findall(r':\s*"((?:[^"\\]|\\.)*)"', m.group(1)))
    return texte


def test_jeder_text_ist_uebersetzt():
    import os

    from app.verwaltung.i18n import WOERTERBUECHER

    os.chdir(pathlib.Path(__file__).parent.parent)
    texte = ui_texte()
    assert len(texte) > 150
    for sprache, buch in WOERTERBUECHER.items():
        fehlt = sorted(texte - set(buch))
        assert not fehlt, f"{sprache}: {len(fehlt)} Texte fehlen, z. B. {fehlt[:5]}"
        for de, fremd in buch.items():  # gleiche Platzhalter
            assert set(re.findall(r"\{(\w+)\}", de)) == set(re.findall(r"\{(\w+)\}", fremd)), de


def test_englisch_per_browser_und_umschalter(client, admin):  # noqa: F811
    seite = client.get("/verwaltung/", headers={"Accept-Language": "en-GB,en;q=0.9"}).text
    assert '<html lang="en">' in seite and "Overview" in seite and "Transcription" in seite and "Übersicht" not in seite
    r = client.get("/verwaltung/sprache/en", params={"weiter": "/verwaltung/konten"}, follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/konten" and "tw_sprache=en" in r.headers["set-cookie"]
    client.cookies.set("tw_sprache", "en")
    assert "Accounts" in client.get("/verwaltung/konten").text
    # Keine fremden Weiterleitungen
    r = client.get("/verwaltung/sprache/de", params={"weiter": "https://boese.example"}, follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/"
    r = client.get("/verwaltung/sprache/de", params={"weiter": "//boese.example"}, follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/"


def test_titel_ohne_html(client, admin):  # noqa: F811
    for seite in ("/", "/transkription", "/zusammenfassung", "/warteschlange", "/konten", "/einstellungen"):
        titel = re.search(r"<title>(.*?)</title>", client.get("/verwaltung" + seite).text, re.S).group(1)
        assert "<" not in titel and len(titel) < 80, (seite, titel)


# ---------------------------------------------------------------- Transkription-Seite
def test_worker_und_extern_auf_einer_seite(client, admin):  # noqa: F811
    seite = client.get("/verwaltung/transkription").text
    assert "Lokaler Worker" in seite and "Alle Worker" in seite and "Externe Transkription" in seite
    alt = client.get("/verwaltung/worker", follow_redirects=False)
    assert alt.status_code == 301 and alt.headers["location"] == "/verwaltung/transkription"
    assert 'href="/verwaltung/transkription" aria-current="page"' in seite


def test_modellfassungen_der_worker(client, dbs, admin):  # noqa: F811
    import json

    from tests.test_step2a import worker_token
    from app.models import Worker

    t = worker_token(dbs)
    w = dbs.get(Worker, t.split(".")[1])
    w.info = json.dumps({"gpu": "RTX 4060", "modell": "large-v3", "modelle": {
        "Systran/faster-whisper-large-v3": "edaa852ec7e1", "pyannote/speaker-diarization-community-1": ""}})
    dbs.commit()
    seite = client.get("/verwaltung/transkription").text
    assert "faster-whisper-large-v3 · edaa852" in seite and "speaker-diarization-community-1 · eigener Ordner" in seite


def test_externe_transkription_ueber_oberflaeche(client, dbs, admin):  # noqa: F811
    from app.einstellungen import extern_konfig

    assert client.get(f"{API}/info").json()["externalTranscription"] is None
    url = "/verwaltung/transkription/extern"
    r = client.post(url, data={"csrf": admin, "anbieter": "mistral", "api_key": "", "stunden": "24"})
    assert r.status_code == 400 and "API-Schlüssel eintragen" in r.text
    r = client.post(url, data={"csrf": admin, "anbieter": "mistral", "api_key": "zu kurz", "stunden": "24"})
    assert r.status_code == 400
    r = client.post(url, data={"csrf": admin, "anbieter": "mistral", "api_key": "sk-abcdefghijklmnop1234",
                               "stunden": "12"})
    assert r.status_code == 200 and "gespeichert" in r.text and "…1234" in r.text
    assert "sk-abcdefghijklmnop1234" not in r.text  # Schlüssel nie wieder anzeigen
    assert client.get(f"{API}/info").json()["externalTranscription"] == "mistral"
    dbs.expire_all()
    k = extern_konfig(dbs)
    assert k.stunden == 12 and k.api_key.endswith("1234") and k.quelle == "verwaltung"
    # Leeres Feld behält den Schlüssel, Abschalten wirkt sofort
    client.post(url, data={"csrf": admin, "anbieter": "", "api_key": "", "stunden": "12"})
    assert client.get(f"{API}/info").json()["externalTranscription"] is None
    dbs.expire_all()
    assert extern_konfig(dbs).api_key.endswith("1234")
    client.post(url, data={"csrf": admin, "anbieter": "", "key_loeschen": "1", "stunden": "12"})
    dbs.expire_all()
    assert extern_konfig(dbs).api_key is None
    assert client.post(url, data={"anbieter": ""}).status_code == 403  # CSRF


# ---------------------------------------------------------------- Einladungsseite
@pytest.fixture()
def einladung(client, world):
    return client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]


def test_einladungsseite_fuer_leute_ohne_app(client, admin, einladung):  # noqa: F811
    client.post("/verwaltung/einstellungen", data={
        "csrf": admin, "server_name": "Taleward", "server_operator": "Drachenhort EV", "min_age": "16",
        "app_download_url": "https://drive.example/taleward-0.9.0.apk"})
    client.cookies.clear()
    r = client.get(f"/einladung/{einladung}")
    link = f"http://testserver/einladung/{einladung}"
    assert f'href="taleward://einladung?url={quote(link, safe="")}"' in r.text.replace("&amp;", "&")
    assert 'href="https://drive.example/taleward-0.9.0.apk"' in r.text and "Play Store" in r.text
    assert f'value="{link}"' in r.text and "gültig bis" in r.text
    assert 'property="og:image" content="http://testserver/verwaltung/static/marke/og-image.png"' in r.text
    assert "Einladung zu Taleward" in r.text and "Rabenfels" not in r.text
    assert "<script" not in r.text
    en = client.get(f"/einladung/{einladung}", headers={"Accept-Language": "en"}).text
    assert "Open in Taleward" in en and "Download the app" in en


def test_einladung_ohne_download_und_abgelaufen(client, dbs, einladung):
    from app.db import utcnow
    from app.models import Invite

    r = client.get(f"/einladung/{einladung}")
    assert "App herunterladen" not in r.text  # kein Link eingestellt → kein Knopf
    dbs.get(Invite, einladung).expires_at = utcnow()
    dbs.commit()
    r = client.get(f"/einladung/{einladung}")
    assert r.status_code == 404 and "gilt nicht mehr" in r.text and "In Taleward öffnen" not in r.text


# ---------------------------------------------------------------- App-Versionen (0.3.9)
def test_versionsvergleich():
    from app.versionen import veraltet

    assert veraltet("0.9.9", "0.10.0") and not veraltet("0.10.0", "0.9.9")
    assert not veraltet("0.9", "0.9.0") and veraltet(None, "0.9.0") and veraltet("kaputt", "0.9.0")
    assert not veraltet(None, None)


def test_mindestversion(client, world, admin):  # noqa: F811
    r = client.post("/verwaltung/einstellungen", data={
        "csrf": admin, "server_name": "Taleward", "server_operator": "Drachenhort EV", "min_age": "16",
        "app_min_version": "0.9.0", "app_latest_version": "0.9.1", "app_release_notes": "Neue Einladungen."})
    assert "Gespeichert" in r.text
    info = client.get(f"{API}/info").json()  # /info bleibt immer erreichbar
    assert info["minAppVersion"] == "0.9.0" and info["latestAppVersion"] == "0.9.1"
    assert info["releaseNotes"] == "Neue Einladungen." and info["apiVersion"] == "0.4.7"
    url = f"{API}/campaigns"
    r = client.get(url, headers=world["gm"])
    assert r.status_code == 426 and "0.9.0" in r.json()["message"]
    r = client.get(url, headers={**world["gm"], "X-Taleward-App": "0.8.5", "Accept-Language": "en"})
    assert r.status_code == 426 and r.json()["code"] == "app_outdated" and "too old" in r.json()["message"]
    assert client.get(url, headers={**world["gm"], "X-Taleward-App": "0.9.0"}).status_code == 200
    assert client.get(url, headers={**world["gm"], "X-Taleward-App": "0.10.0"}).status_code == 200
    # Verwaltung und Einladungsseite sind nicht betroffen
    assert client.get("/verwaltung/einstellungen").status_code == 200
    r = client.post("/verwaltung/einstellungen", data={
        "csrf": admin, "server_name": "Taleward", "server_operator": "Drachenhort EV", "min_age": "16",
        "app_min_version": "neun"})
    assert r.status_code == 400
    # CORS: Browser-App darf den Header schicken, 426 trägt CORS-Kopfzeilen
    pre = client.options(url, headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "x-taleward-app,authorization"})
    assert pre.status_code == 200 and "x-taleward-app" in pre.headers["access-control-allow-headers"].lower()
    r = client.get(url, headers={**world["gm"], "Origin": "http://localhost:5173"})
    assert r.status_code == 426 and r.headers.get("access-control-allow-origin") == "http://localhost:5173"
