"""Eingaben und Grenzen (Server 0.4.37): Modell-Downloads, Aufträge, Anfragegrößen, Audioformate, Wissen einzelner,
Chronik von Ehemaligen, Links in Mails, Export-Schlüssel, Versuchsgrenzen, Abmelden."""
import re
import shutil

import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


# ---------------------------------------------------------------- Modell-Download
@pytest.mark.parametrize("pfad", ["C:/x.bat", "C:x", "/x", "a/../../x", "a\\b", "..", "a//b", "", "a:b", "./x"])
def test_modellpfade_abgelehnt(pfad):
    from app.modelle import _sicher

    assert not _sicher(pfad)


def test_modellpfade_erlaubt():
    from app.modelle import _sicher, commit_ok

    assert _sicher("config.yaml") and _sicher("embedding/pytorch_model.bin")
    assert commit_ok("a" * 40) and not commit_ok("main") and not commit_ok("../../x") and not commit_ok("A" * 40)


def test_fremde_fassung_loescht_nichts(tmp_path, monkeypatch):
    from app import config, modelle

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "daten"))
    config.get_settings.cache_clear()
    opfer = tmp_path / "daten" / "wichtig"
    opfer.mkdir(parents=True)
    (opfer / "datei.txt").write_text("bleibt")

    class Quelle:
        def verzeichnis(self, repo):
            return {"fassung": "../../wichtig", "dateien": []}

    with pytest.raises(modelle.ModellFehler):
        modelle.vom_server("org/modell", Quelle())
    assert (opfer / "datei.txt").read_text() == "bleibt"

    class Quelle2:
        def verzeichnis(self, repo):
            return {"fassung": "b" * 40, "dateien": [{"pfad": "C:/Users/x/a.bat", "sha256": "0"}]}

        def laden(self, *a):
            raise AssertionError("darf nicht laden")

    with pytest.raises(modelle.ModellFehler):
        modelle.vom_server("org/modell", Quelle2())
    config.get_settings.cache_clear()


# ---------------------------------------------------------------- Aufträge an den Worker
@pytest.mark.parametrize("datei", [
    {"fileId": "../x", "position": 0, "chunks": []},
    {"fileId": "a/b", "position": 0, "chunks": []},
    {"fileId": "f1", "position": "0", "chunks": []},
    {"fileId": "f1", "position": 0, "chunks": [{"url": "https://anderswo.example/x"}]},
    {"fileId": "f1", "position": 0, "chunks": [{"url": "//anderswo.example/worker/v1/x"}]},
])
def test_auftrag_mit_fremden_angaben_abgelehnt(datei):
    from app.audio import AudioFehler
    from app.worker_prozess import auftrag_pruefen

    with pytest.raises(AudioFehler):
        auftrag_pruefen({"files": [datei]})


def test_auftrag_normal_angenommen():
    from app.worker_prozess import auftrag_pruefen

    auftrag_pruefen({"files": [{"fileId": "0f8fad5b-d9cb-469f-a165-70867728950e", "position": 0,
                                "chunks": [{"url": "/worker/v1/jobs/j/files/f/chunks/0"}]},
                               {"fileId": "stimme", "position": 1, "chunks": []}]})


# ---------------------------------------------------------------- Anfragegrößen
def test_grosser_teil_ohne_laenge_wird_abgewiesen(client, world):
    from tests.test_step2a import neue_session

    w = world
    s = neue_session(client, w)
    up = client.post(f"{API}/sessions/{s['id']}/uploads", headers=w["gm"], json={
        "source": "table", "files": [{"fileName": "a.m4a", "sizeBytes": 20 * 1024 * 1024, "mimeType": "audio/mp4"}]
    }).json()
    datei = up["files"][0]["fileId"]

    def strom():
        for _ in range(8):
            yield b"x" * (1024 * 1024)  # 8 MB, Teile sind höchstens 5 MB

    r = client.put(f"{API}/uploads/{up['uploadId']}/files/{datei}/chunks/0", content=strom(),
                   headers={**w["gm"], "Content-Type": "application/octet-stream"})
    assert r.status_code == 413


def test_obergrenze_jeder_anfrage():
    from fastapi import FastAPI, Request

    from app.koerper import Grenze

    app = FastAPI()

    @app.post("/x")
    async def x(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(Grenze, max_bytes=1000)
    c = TestClient(app)
    assert c.post("/x", content=b"a" * 1000).json() == {"n": 1000}
    r = c.post("/x", content=b"a" * 1001)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    r = c.post("/x", content=(b"a" * 400 for _ in range(4)))  # ohne Content-Length
    assert r.status_code == 413


def test_caddy_begrenzt():
    from pathlib import Path

    assert "max_size" in (Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile").read_text()


# ---------------------------------------------------------------- Audioformate
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg fehlt")
def test_wiedergabeliste_wird_nicht_geoeffnet(tmp_path):
    from app import audio

    datei = tmp_path / "aufnahme.audio"
    datei.write_text("#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n")
    with pytest.raises(audio.AudioFehler):
        audio.dekodieren(datei)
    assert "-protocol_whitelist" in audio.EINGABE and "-format_whitelist" in audio.EINGABE


# ---------------------------------------------------------------- Wissen einzelner
def test_teilweise_verborgenes_gilt_als_geheim(client, world, dbs):
    from app.models import Entry, GameSession
    from app.zusammenfassung import eingabe_bauen, geheime_bibeltexte, vorschlag_eingabe

    w = world
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Der Spitzel", "summary": "NUR-FUER-EINIGE", "visibility": "public",
        "hiddenFromMemberIds": [w["pl_member"]]})
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Markt", "summary": "Laut.", "visibility": "public"})
    from tests.test_step2a import neue_session

    s = neue_session(client, w)
    sitzung = dbs.get(GameSession, s["id"])
    ein = vorschlag_eingabe(dbs, sitzung, eingabe_bauen(dbs, sitzung))
    assert {e["name"] for e in ein["bibel"]} == {"Markt"} | ({"Mira"} & {e["name"] for e in ein["bibel"]})
    assert "Der Spitzel" in {e["name"] for e in ein["geheim"]}
    assert "NUR-FUER-EINIGE" in geheime_bibeltexte(dbs.query(Entry).all())


# ---------------------------------------------------------------- Chronik nach dem Verlassen
def test_chronik_ehemaliger_ohne_spaeteres(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/members/me/chronicle"
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"])
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Miras Verfolger", "summary": "Jagt Mira.", "visibility": "public"})
    namen = {m["name"] for m in client.get(url, headers=w["pl"]).json()["mentions"]}
    assert "Miras Verfolger" not in namen


# ---------------------------------------------------------------- Links in Mails
def test_ohne_oeffentliche_adresse_keine_mail(client, world, dbs, monkeypatch):
    from app import mail
    from app.einstellungen import meta_schreiben
    from app.models import User

    gesendet = []
    monkeypatch.setattr(mail, "senden", lambda db, an, betreff, text: gesendet.append(text))
    meta_schreiben(dbs, "melden.smtp_host", "smtp.example.org")
    ben = dbs.query(User).filter_by(username="ben").one()
    ben.email = "ben@example.org"
    dbs.commit()
    assert client.get(f"{API}/info").json()["passwordReset"] is False
    r = client.post(f"{API}/auth/password-reset", json={"login": "ben"}, headers={"Host": "boese.example"})
    assert r.status_code == 202 and gesendet == []
    assert client.put(f"{API}/me/email", json={"email": "ben2@example.org"}, headers=w_pl(client)).status_code == 503


def w_pl(client):
    r = client.post(f"{API}/auth/login", json={"username": "ben", "password": "geheim123"})
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


# ---------------------------------------------------------------- Einladungen und Anmelden
def test_einladungscode_lang_genug(client, world):
    w = world
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    assert re.fullmatch(r"[A-Z]+-\d{8}", code)


def test_falsche_einladungscodes_werden_gebremst(client, world):
    w = world
    for i in range(10):
        r = client.post(f"{API}/campaigns/join", json={"code": f"RABE-{i:08d}"}, headers=w["out"])
        assert r.status_code == 404
    r = client.post(f"{API}/campaigns/join", json={"code": "RABE-99999999"}, headers=w["out"])
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"  # 0.4.9 (vorher 409)


def test_falsche_passwoerter_werden_gebremst(client, make_user):
    make_user("dora")
    for _ in range(10):
        assert client.post(f"{API}/auth/login", json={"username": "dora", "password": "falsch"}).status_code == 401
    r = client.post(f"{API}/auth/login", json={"username": "dora", "password": "geheim123"})
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"  # 0.4.9; auch das richtige Passwort wartet, bis das Fenster abgelaufen ist


def test_versuchszaehler_wachsen_nicht_unbegrenzt(monkeypatch):
    from app import begrenzung

    monkeypatch.setattr(begrenzung, "MAX_SCHLUESSEL", 100)
    z = begrenzung.Zaehler()
    for i in range(1000):
        z.zaehlen(f"k{i}", 60)
    assert len(z._daten) == 100


# ---------------------------------------------------------------- Abmelden der Verwaltung
def test_abmelden_macht_alte_sitzung_ungueltig(client, dbs):
    import time

    from app.cli import _neues_konto

    _neues_konto(dbs, "chef", "Chefin", "geheim123", admin=True)
    dbs.commit()
    client.post("/verwaltung/anmelden", data={"username": "chef", "password": "geheim123"}, follow_redirects=False)
    alt = dict(client.cookies)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/verwaltung/").text).group(1)
    time.sleep(1.1)
    client.post("/verwaltung/abmelden", data={"csrf": csrf}, follow_redirects=False)
    client.cookies.clear()
    for k, v in alt.items():
        client.cookies.set(k, v, path="/verwaltung")
    assert client.get("/verwaltung/", follow_redirects=False).status_code == 303


def test_leichtes_passwort_in_der_verwaltung_abgelehnt(client, dbs):
    from app.cli import _neues_konto

    _neues_konto(dbs, "chef", "Chefin", "geheim123", admin=True)
    dbs.commit()
    client.post("/verwaltung/anmelden", data={"username": "chef", "password": "geheim123"}, follow_redirects=False)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/verwaltung/").text).group(1)
    r = client.post("/verwaltung/konten", data={"csrf": csrf, "username": "lilio", "display_name": "Lilio",
                                                "password": "passwort1"})
    assert "zu leicht" in r.text


# ---------------------------------------------------------------- Kopfzeilen
def test_web_app_mit_csp_und_hsts(client):
    r = client.get("/app/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers  # http (Heimnetz)
    r = TestClient.get(client, "https://testserver/verwaltung/anmelden")
    assert r.headers["strict-transport-security"].startswith("max-age=")
