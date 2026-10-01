"""Einrichtung mit Code, Einrichtungsassistent, Worker koppeln, Hugging-Face-Zugang, Kostenlimit, Sicherung."""
import io
import re
import zipfile

import pytest

from tests.test_verwaltung import admin, csrf_von  # noqa: F401 (Fixture)

API = "/api/v1"


@pytest.fixture()
def schluessel_ok(monkeypatch):
    """Anbieter-Prüfung nachgebaut: Ergebnis je Anbieter einstellbar."""
    from app import schluessel

    stand = {"mistral": "ok", "hf": "ok"}
    monkeypatch.setattr(schluessel, "mistral", lambda key: stand["mistral"])
    monkeypatch.setattr(schluessel, "huggingface", lambda token: stand["hf"])
    return stand


# ---------------------------------------------------------------- Einrichtung mit Code
def test_einrichtung_nur_mit_code(client, dbs):
    from typer.testing import CliRunner

    from app import einrichtung
    from app.cli import app

    code = einrichtung.beim_start(dbs)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    seite = client.get("/verwaltung/anmelden")  # → Einrichtung
    assert "Einrichtungscode" in seite.text and "chronik einrichtungscode" in seite.text
    assert "Dieser Einrichtungscode stimmt nicht" in client.get("/verwaltung/einrichtung?code=FALSCH-00").text
    body = {"username": "markus", "display_name": "Markus", "password": "Drachenfeuer7", "password2": "Drachenfeuer7"}
    assert client.post("/verwaltung/einrichtung", data={**body, "code": "FALSCH"}).status_code == 400
    r = client.post("/verwaltung/einrichtung", data={**body, "code": code, "password2": "anders123"})
    assert r.status_code == 400 and "stimmen nicht" in r.text
    assert 'name="username"' in client.get(f"/verwaltung/einrichtung?code={code.lower()}").text
    r = client.post("/verwaltung/einrichtung", data={**body, "code": code}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/verwaltung/assistent/verein"
    assert "tw_verwaltung" in r.headers["set-cookie"]
    assert "Euer Verein" in client.get("/verwaltung/assistent/verein").text
    # Code verbraucht, Server eingerichtet
    client.cookies.clear()
    assert client.get(f"/verwaltung/einrichtung?code={code}", follow_redirects=False).headers["location"] \
        == "/verwaltung/anmelden"
    assert CliRunner().invoke(app, ["einrichtungscode"]).exit_code == 1
    assert client.post(f"{API}/auth/login", json={"username": "markus", "password": "Drachenfeuer7"}).status_code == 200


def test_einrichtungscode_raten_wird_gebremst(client, dbs):
    from app import einrichtung

    code = einrichtung.beim_start(dbs)
    body = {"username": "x", "display_name": "X", "password": "Drachenfeuer7", "password2": "Drachenfeuer7"}
    for i in range(5):
        client.post("/verwaltung/einrichtung", data={**body, "code": f"RATE-{i:04d}"})
    r = client.post("/verwaltung/einrichtung", data={**body, "code": code})
    assert "Zu viele Fehlversuche" in r.text


def test_einrichtungscode_per_cli(client, dbs):
    from typer.testing import CliRunner

    from app.cli import app

    r = CliRunner().invoke(app, ["einrichtungscode"])
    assert r.exit_code == 0 and "/verwaltung/einrichtung?code=" in r.output
    alt = re.search(r"code=([A-Z0-9-]+)", r.output).group(1)
    neu = re.search(r"code=([A-Z0-9-]+)", CliRunner().invoke(app, ["einrichtungscode", "--neu"]).output).group(1)
    assert alt != neu
    assert "stimmt nicht" in client.get(f"/verwaltung/einrichtung?code={alt}").text


# ---------------------------------------------------------------- Assistent
def test_assistent_nur_cloud(client, dbs, admin, schluessel_ok, make_user, login):  # noqa: F811
    from app.einstellungen import extern_konfig, llm_konfig

    r = client.post("/verwaltung/assistent/verein", data={"csrf": admin, "verein_name": "Drachenhort e. V.",
                                                          "server_contact": "", "min_age": "16"})
    assert r.status_code == 400 and "Kontaktadresse" in r.text
    r = client.post("/verwaltung/assistent/verein", data={"csrf": admin, "verein_name": "Drachenhort e. V.",
                                                          "server_contact": "ds@drachenhort.example", "min_age": "16"},
                    follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/assistent/betrieb"
    assert client.get(f"{API}/info").json()["operator"] == "Drachenhort e. V."
    assert "Nur Cloud" in client.get("/verwaltung/assistent/betrieb").text
    client.post("/verwaltung/assistent/betrieb/art", data={"csrf": admin, "art": "cloud"})
    seite = client.get("/verwaltung/assistent/betrieb").text
    assert "Monatliches Kostenlimit" in seite and "Hugging Face" not in seite
    schluessel_ok["mistral"] = "falsch"
    r = client.post("/verwaltung/assistent/betrieb", data={"csrf": admin, "mistral_key": "sk-falsch-123456789",
                                                           "limit_euro": "20"})
    assert r.status_code == 400 and "lehnt diesen API-Schlüssel ab" in r.text
    schluessel_ok["mistral"] = "ok"
    r = client.post("/verwaltung/assistent/betrieb", data={"csrf": admin, "mistral_key": "sk-richtig-123456789",
                                                           "limit_euro": "20"}, follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/assistent/zusammenfassung"
    k = extern_konfig(dbs)
    assert (k.anbieter, k.stunden, k.api_key) == ("mistral", 0.0, "sk-richtig-123456789")
    assert client.get(f"{API}/info").json()["externalTranscription"] == "mistral"
    client.post("/verwaltung/assistent/zusammenfassung", data={"csrf": admin, "wahl": "api"})
    dbs.expire_all()
    llm = llm_konfig(dbs)
    assert (llm.art, llm.api_key, llm.api_modell) == ("api", "sk-richtig-123456789", "mistral-large-latest")
    # Neue Kampagnen dürfen in dieser Betriebsart gleich in die Cloud
    make_user("sl")
    c = client.post(f"{API}/campaigns", headers=login("sl"), json={"title": "Rabenfels"}).json()
    assert c["allowExternalTranscription"] is True


def test_assistent_lokaler_server(client, dbs, admin, schluessel_ok):  # noqa: F811
    from fastapi.testclient import TestClient

    client.post("/verwaltung/assistent/betrieb/art", data={"csrf": admin, "art": "lokal"})
    seite = client.get("/verwaltung/assistent/betrieb").text
    assert "Hugging Face" in seite and "Mistral" not in seite
    schluessel_ok["hf"] = "kein_zugang"
    r = client.post("/verwaltung/assistent/betrieb", data={"csrf": admin, "hf_token": "hf_abcdefghijklmnop"})
    assert r.status_code == 400 and "Modellbedingungen" in r.text
    schluessel_ok["hf"] = "ok"
    client.post("/verwaltung/assistent/betrieb", data={"csrf": admin, "hf_token": "hf_abcdefghijklmnop"})
    assert "endet auf …mnop" in client.get("/verwaltung/assistent/betrieb").text
    # Worker koppeln
    client.post("/verwaltung/assistent/koppeln", data={"csrf": admin, "weiter": "/verwaltung/assistent/betrieb"})
    seite = client.get("/verwaltung/assistent/betrieb").text
    code = re.search(r"--koppeln ([A-Z2-9-]+)", seite).group(1)
    assert '<meta http-equiv="refresh"' in seite  # wartet auf den Worker
    wc = TestClient(client.app)
    assert wc.post("/worker/v1/pair", json={"code": "FALS-CHES", "name": "x"}).json()["code"] == "pairing_code_invalid"
    r = wc.post("/worker/v1/pair", json={"code": code.lower(), "name": "gaming-pc"})
    assert r.status_code == 201 and r.json()["name"] == "gaming-pc"
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert wc.post("/worker/v1/pair", json={"code": code, "name": "zweiter"}).status_code == 404  # nur einmal
    assert wc.get("/worker/v1/config", headers=h).json() == {"hfToken": "hf_abcdefghijklmnop", "models": {}, "serverVersion": "0.4.32"}
    assert wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers=h).status_code == 204
    seite = client.get("/verwaltung/assistent/betrieb").text
    assert "gaming-pc" in seite and "bereit" in seite
    # Lokales Modell wählbar
    assert "Lokales Modell" in client.get("/verwaltung/assistent/zusammenfassung").text


def test_koppeln_schreibt_env(tmp_path):
    from app.cli import _env_setzen

    datei = tmp_path / ".env"
    datei.write_text("# WORKER_TOKEN=\nHF_TOKEN=hf_x\nWORKER_SERVER_URL=http://alt\n")
    _env_setzen({"WORKER_TOKEN": "wk.a.b", "WORKER_SERVER_URL": "https://neu"}, datei)
    assert datei.read_text() == "WORKER_TOKEN=wk.a.b\nHF_TOKEN=hf_x\nWORKER_SERVER_URL=https://neu\n"


def test_datenschutz(client, dbs, admin, schluessel_ok):  # noqa: F811
    client.post("/verwaltung/assistent/verein", data={"csrf": admin, "verein_name": "Drachenhort e. V.",
                                                      "server_contact": "ds@drachenhort.example", "min_age": "16"})
    client.post("/verwaltung/assistent/betrieb/art", data={"csrf": admin, "art": "cloud"})
    client.post("/verwaltung/assistent/betrieb", data={"csrf": admin, "mistral_key": "sk-richtig-123456789",
                                                       "limit_euro": "0"})
    seite = client.get("/verwaltung/assistent/datenschutz").text
    text = re.search(r'<textarea name="text"[^>]*>(.*?)</textarea>', seite, re.S).group(1)
    import html
    text = html.unescape(text)
    assert "Drachenhort e. V." in text and "Mistral AI" in text and "ds@drachenhort.example" in text
    assert client.get("/datenschutz").status_code == 404
    r = client.post("/verwaltung/assistent/datenschutz", data={"csrf": admin, "aktion": "veroeffentlichen", "text": text})
    assert r.status_code == 400 and "eckigen Klammern" in r.text
    fertig = re.sub(r"\[[^\]]*\]", "ausgefüllt", text)
    r = client.post("/verwaltung/assistent/datenschutz", data={"csrf": admin, "aktion": "veroeffentlichen",
                                                               "text": fertig + "\n\n<script>x</script>"},
                    follow_redirects=False)
    assert r.headers["location"] == "/verwaltung/assistent/sicherung"
    oeffentlich = client.get("/datenschutz")
    assert oeffentlich.status_code == 200 and "<h2>Deine Rechte</h2>" in oeffentlich.text
    assert "<script>x" not in oeffentlich.text and "&lt;script&gt;" in oeffentlich.text
    assert client.get(f"{API}/info").json()["privacyPolicyUrl"] == "http://testserver/datenschutz"
    r = client.post("/verwaltung/assistent/datenschutz", data={"csrf": admin, "aktion": "link",
                                                               "eigener_link": "https://verein.example/ds"})
    assert client.get(f"{API}/info").json()["privacyPolicyUrl"] == "https://verein.example/ds"


def test_checkliste_und_spielleitung(client, dbs, admin):  # noqa: F811
    seite = client.get("/verwaltung/").text
    assert re.search(r"Einrichtung: \d von \d erledigt", seite) and "Einrichtung fortsetzen" in seite
    assert client.get("/verwaltung/assistent", follow_redirects=False).headers["location"] \
        == "/verwaltung/assistent/verein"
    r = client.post("/verwaltung/assistent/spielleitung", data={"csrf": admin, "username": "lena",
                                                                "display_name": "Lena", "password": "Startwort-9"})
    assert "Konto „lena“ angelegt" in r.text
    fertig = client.get("/verwaltung/assistent/fertig").text
    assert "✓" in fertig and "Erste Spielleitung" in fertig
    client.post("/verwaltung/assistent/ausblenden", data={"csrf": admin})
    assert "Einrichtung fortsetzen" not in client.get("/verwaltung/").text


# ---------------------------------------------------------------- Kostenlimit
def test_kostenlimit(client, world, dbs, admin, monkeypatch):  # noqa: F811
    from app import extern, kosten
    from app.einstellungen import meta_schreiben
    from app.models import UsageLog
    from app.zusammenfassung import einen_auftrag

    meta_schreiben(dbs, "kosten.limit_cent", "100")
    meta_schreiben(dbs, "extern.anbieter", "mistral")
    meta_schreiben(dbs, "extern.api_key", "sk-test-123456789")
    meta_schreiben(dbs, "llm.art", "api")
    dbs.add(UsageLog(campaign_id=world["cid"], kind="transcription", engine="external", cost_cents=60))
    dbs.commit()
    assert not kosten.erreicht(dbs) and kosten.monat_cent(dbs) == 60
    dbs.add(UsageLog(campaign_id=world["cid"], kind="summary", engine="external", cost_cents=40))
    dbs.add(UsageLog(campaign_id=world["cid"], kind="transcription", engine="local", cost_cents=500))  # zählt nicht
    dbs.commit()
    assert kosten.erreicht(dbs)
    assert not extern.einen_auftrag(dbs) and not einen_auftrag(dbs)
    seite = client.get("/verwaltung/").text
    assert "Diesen Monat: 1.00 €" in seite and "Limit ist erreicht" in seite
    r = client.post("/verwaltung/einstellungen", data={"csrf": admin, "server_name": "S", "server_operator": "V",
                                                       "min_age": "16", "limit_euro": "5"})
    assert r.status_code == 200
    dbs.expire_all()
    assert kosten.limit_cent(dbs) == 500 and not kosten.erreicht(dbs)


# ---------------------------------------------------------------- Sicherung
def test_sicherung_vollstaendig_und_wiederherstellen(client, world, dbs, admin, tmp_path):  # noqa: F811
    from app import sicherung
    from app.config import get_settings
    from app.models import Campaign
    from tests.test_konto import jpeg

    w = world
    client.put(f"{API}/campaigns/{w['cid']}/cover-image", headers={**w["gm"], "Content-Type": "image/jpeg"},
               content=jpeg(100, 100))
    extra = tmp_path / "storagebox"
    extra.mkdir()
    r = client.post("/verwaltung/sicherung/einstellungen", data={"csrf": admin, "auto": "1", "tage": "7",
                                                                 "ordner": "relativ/pfad"})
    assert r.status_code == 400
    client.post("/verwaltung/sicherung/einstellungen", data={"csrf": admin, "auto": "1", "tage": "7",
                                                             "ordner": str(extra)})
    r = client.post("/verwaltung/sicherung", data={"csrf": admin})
    assert "Sicherung angelegt" in r.text
    [e] = sicherung.liste()
    assert (extra / e.name).exists()
    datei = client.get(f"/verwaltung/sicherungen/{e.name}")
    assert datei.status_code == 200
    with zipfile.ZipFile(io.BytesIO(datei.content)) as z:
        namen = z.namelist()
    assert "chronik.db" in namen and "manifest.json" in namen and any(n.startswith("bilder/") for n in namen)
    assert client.get("/verwaltung/sicherungen/..%2Fchronik.db").status_code == 404
    # Automatisch: erst nach 24 Stunden wieder
    assert sicherung.automatisch(dbs) is None
    # Wiederherstellen
    client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"title": "Geändert"})
    dbs.close()
    from app import db
    db.reset_engine()
    vorher = sicherung.wiederherstellen(sicherung.pfad(e.name))
    assert vorher and vorher.exists()
    db.reset_engine()
    from app.db import session_factory
    with session_factory()() as neu:
        assert neu.get(Campaign, w["cid"]).title == "Rabenfels"
    assert (get_settings().data_dir / "bilder").exists()
    kaputt = tmp_path / "kaputt.zip"
    kaputt.write_bytes(b"kein zip")
    with pytest.raises(sicherung.SicherungFehler):
        sicherung.pruefen(kaputt)
