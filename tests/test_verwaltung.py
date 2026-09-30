"""Weboberfläche /verwaltung: Anmeldung, Schutz, Konten, Worker (auch eigener), Warteschlange, Einstellungen."""
import re
import sys
import time

import pytest

API = "/api/v1"


def csrf_von(html: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


@pytest.fixture()
def admin(client, dbs):
    from app.cli import _neues_konto

    _neues_konto(dbs, "chef", "Chefin", "geheim123", admin=True)
    _neues_konto(dbs, "mitglied", "Mitglied", "geheim123")
    dbs.commit()
    r = client.post("/verwaltung/anmelden", data={"username": "chef", "password": "geheim123"},
                    follow_redirects=False)
    assert r.status_code == 303 and "tw_verwaltung" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    return csrf_von(client.get("/verwaltung/").text)


def test_anmeldung_und_schutz(client, dbs):
    from app.cli import _neues_konto

    _neues_konto(dbs, "mitglied", "Mitglied", "geheim123")
    dbs.commit()
    r = client.get("/verwaltung/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/verwaltung/anmelden"
    seite = client.get("/verwaltung/anmelden")
    assert "chronik einrichtungscode" in seite.text  # noch kein Verwalter → Einrichtung mit Code
    assert "frame-ancestors 'none'" in seite.headers["content-security-policy"]
    r = client.post("/verwaltung/anmelden", data={"username": "mitglied", "password": "geheim123"})
    assert r.status_code == 400 and "kein Verwalter-Recht" in r.text
    for _ in range(5):
        client.post("/verwaltung/anmelden", data={"username": "mitglied", "password": "falsch"})
    r = client.post("/verwaltung/anmelden", data={"username": "mitglied", "password": "geheim123"})
    assert "Zu viele Fehlversuche" in r.text
    # App-Token öffnet die Verwaltung nicht
    token = client.post(f"{API}/auth/login", json={"username": "mitglied", "password": "geheim123"}).json()
    r = client.get("/verwaltung/", headers={"Authorization": f"Bearer {token['accessToken']}"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).headers["location"] == "/verwaltung"


def test_csrf_und_abmelden(client, admin):
    r = client.post("/verwaltung/konten", data={"username": "x", "display_name": "X", "password": "geheim123"})
    assert r.status_code == 403  # ohne CSRF-Merkmal
    r = client.post("/verwaltung/konten", data={"csrf": "0" * 40, "username": "x", "display_name": "X",
                                                "password": "geheim123"})
    assert r.status_code == 403
    r = client.post("/verwaltung/abmelden", data={"csrf": admin}, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()
    assert client.get("/verwaltung/", follow_redirects=False).status_code == 303


def test_konten(client, dbs, admin):
    r = client.post("/verwaltung/konten", data={"csrf": admin, "username": "Lilio", "display_name": "Lilio",
                                                "password": "kurz"})
    assert r.status_code == 400 and "mindestens 8" in r.text
    r = client.post("/verwaltung/konten", data={"csrf": admin, "username": "Lilio", "display_name": "Lilio",
                                                "password": "geheim123"})
    assert r.status_code == 200 and "Konto angelegt" in r.text
    assert client.post(f"{API}/auth/login", json={"username": "lilio", "password": "geheim123"}).status_code == 200
    doppelt = client.post("/verwaltung/konten", data={"csrf": admin, "username": "lilio", "display_name": "L",
                                                      "password": "geheim123"})
    assert "gibt es schon" in doppelt.text
    # Passwort neu setzen → alte App-Anmeldung ungültig
    from app.models import User
    lilio = dbs.query(User).filter_by(username="lilio").one()
    alt = client.post(f"{API}/auth/login", json={"username": "lilio", "password": "geheim123"}).json()["accessToken"]
    client.post(f"/verwaltung/konten/{lilio.id}/passwort", data={"csrf": admin, "password": "neuespw123"})
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {alt}"}).status_code == 401
    assert client.post(f"{API}/auth/login", json={"username": "lilio", "password": "neuespw123"}).status_code == 200
    # Verwalter-Recht umschalten, eigenes nicht entziehbar
    client.post(f"/verwaltung/konten/{lilio.id}/verwalter", data={"csrf": admin})
    assert "Verwalter-Recht entziehen" in client.get("/verwaltung/konten").text
    chef = dbs.query(User).filter_by(username="chef").one()
    r = client.post(f"/verwaltung/konten/{chef.id}/verwalter", data={"csrf": admin})
    assert r.status_code == 400


def test_konto_loeschen_durch_verwalter(client, dbs, admin):
    from app.models import Campaign, Member, User

    client.post("/verwaltung/konten", data={"csrf": admin, "username": "mara", "display_name": "Mara",
                                            "password": "geheim123"})
    mara = dbs.query(User).filter_by(username="mara").one()
    mara_id = mara.id
    token = client.post(f"{API}/auth/login", json={"username": "mara", "password": "geheim123"}).json()["accessToken"]
    # Falscher Bestätigungsname → nichts passiert
    r = client.post(f"/verwaltung/konten/{mara.id}/loeschen", data={"csrf": admin, "bestaetigung": "marra"})
    assert r.status_code == 400 and "„mara“" in r.text
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # Einzige Spielleitung mit weiteren Mitgliedern → Hinweis, Konto bleibt
    chef = dbs.query(User).filter_by(username="chef").one()
    from app.services import default_organization

    c = Campaign(organization_id=default_organization(dbs).id, title="Nebelpfad", language="de")
    dbs.add(c)
    dbs.flush()
    dbs.add_all([Member(campaign_id=c.id, user_id=mara.id, role="gm"),
                 Member(campaign_id=c.id, user_id=chef.id, role="player")])
    dbs.commit()
    r = client.post(f"/verwaltung/konten/{mara.id}/loeschen", data={"csrf": admin, "bestaetigung": "Mara"})
    assert r.status_code == 400 and "einzige Spielleitung" in r.text and "Nebelpfad" in r.text
    dbs.query(Member).filter_by(campaign_id=c.id, user_id=chef.id).one().role = "gm"
    dbs.commit()
    # Jetzt klappt es; die App-Anmeldung ist weg, das Mitglied bleibt als „gelöschtes Konto“
    r = client.post(f"/verwaltung/konten/{mara.id}/loeschen", data={"csrf": admin, "bestaetigung": " MARA "})
    assert r.status_code == 200 and "Konto gelöscht" in r.text
    dbs.expire_all()
    assert dbs.get(User, mara_id) is None
    assert client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    m = dbs.query(Member).filter_by(campaign_id=c.id, role="gm").filter(Member.user_id.is_(None)).one()
    assert m.deleted_at is not None
    # Das eigene Konto nicht hier
    assert client.post(f"/verwaltung/konten/{chef.id}/loeschen", data={"csrf": admin, "bestaetigung": "chef"}).status_code == 400
    assert dbs.get(User, chef.id) is not None


def test_worker_anlegen_pausieren_sperren(client, dbs, admin):
    from fastapi.testclient import TestClient

    r = client.post("/verwaltung/worker", data={"csrf": admin, "name": "pc-von-lilio"})
    token = re.search(r"(wk\.[\w-]+\.[\w-]+)", r.text).group(1)
    assert "nur dieses eine Mal" in r.text
    assert token not in client.get("/verwaltung/transkription").text  # nur einmal angezeigt
    wc = TestClient(client.app)
    h = {"Authorization": f"Bearer {token}"}
    assert wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers=h).status_code == 204
    from app.models import Worker
    w = dbs.query(Worker).filter_by(name="pc-von-lilio").one()
    assert "bereit" in client.get("/verwaltung/transkription").text
    client.post(f"/verwaltung/worker/{w.id}/pause", data={"csrf": admin})
    assert "pausiert" in client.get("/verwaltung/transkription").text
    dbs.expire_all()
    assert dbs.get(Worker, w.id).paused
    client.post(f"/verwaltung/worker/{w.id}/fortsetzen", data={"csrf": admin})
    client.post(f"/verwaltung/worker/{w.id}/sperren", data={"csrf": admin})
    assert wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers=h).status_code == 401


def test_pausierter_worker_bekommt_keine_auftraege(client, world, dbs, tmp_path):
    from fastapi.testclient import TestClient

    from app.models import Worker
    from tests.test_step2a import audio_abschnitte, hochladen, neue_session, worker_token

    s = neue_session(client, world)
    hochladen(client, world["gm"], s["id"], audio_abschnitte(tmp_path, (6,)))
    token = worker_token(dbs)
    w = dbs.get(Worker, token.split(".")[1])
    w.paused = True
    dbs.commit()
    wc = TestClient(client.app)
    h = {"Authorization": f"Bearer {token}"}
    assert wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers=h).status_code == 204
    w.paused = False
    dbs.commit()
    assert wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers=h).status_code == 200


def test_eigener_worker_starten(client, dbs, admin, monkeypatch):
    """Start/Stop über die Verwaltung – mit einem Ersatzprogramm statt des echten Workers."""
    from app.models import Worker
    from app.verwaltung import lokaler_worker

    def ersatz(modus, server_url):
        code = ("import os, signal, sys, time\n"
                "signal.signal(signal.SIGINT, lambda *a: (print('beendet', flush=True), sys.exit(0)))\n"
                f"print('start', {modus!r}, {server_url!r}, os.environ['WORKER_TOKEN'][:3], flush=True)\n"
                "time.sleep(60)\n")
        return [sys.executable, "-c", code]

    knecht = lokaler_worker.LokalerKnecht(befehl=ersatz)
    monkeypatch.setattr(lokaler_worker, "KNECHT", knecht)
    from app.verwaltung import router as vrouter
    monkeypatch.setattr(vrouter, "KNECHT", knecht)

    r = client.post("/verwaltung/worker/lokal/start", data={"csrf": admin, "modus": "attrappe",
                                                                   "autostart": "1"})
    assert r.status_code == 200 and "läuft" in r.text
    w = dbs.query(Worker).filter_by(local=True).one()
    assert w.name == "Lokaler Worker" and w.revoked_at is None
    for _ in range(50):
        if any("start" in z for z in knecht.log_ende()):
            break
        time.sleep(0.1)
    assert any("start attrappe http://127.0.0.1:8000 wk." in z for z in knecht.log_ende())
    from app.einstellungen import meta_lesen
    dbs.expire_all()
    assert meta_lesen(dbs, "lokaler_worker") == "attrappe"
    r = client.post("/verwaltung/worker/lokal/stop", data={"csrf": admin})
    assert not knecht.laeuft() and any("beendet" in z for z in knecht.log_ende())
    dbs.expire_all()
    assert meta_lesen(dbs, "lokaler_worker") == "aus"
    # Neustart erzeugt einen neuen Schlüssel für denselben Eintrag
    alter_hash = dbs.query(Worker).filter_by(local=True).one().token_hash
    knecht.starten(dbs, "echt")
    try:
        dbs.expire_all()
        assert dbs.query(Worker).filter_by(local=True).one().token_hash != alter_hash
        assert dbs.query(Worker).filter_by(local=True).count() == 1
    finally:
        knecht.beenden()


def test_warteschlange_und_neustart(client, world, dbs, admin, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_step2a import audio_abschnitte, hochladen, neue_session, worker_token

    s = neue_session(client, world)
    hochladen(client, world["gm"], s["id"], audio_abschnitte(tmp_path, (6,)))
    t = worker_token(dbs)
    wc = TestClient(client.app)
    a = wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0}, headers={"Authorization": f"Bearer {t}"}).json()
    wc.post(f"/worker/v1/jobs/{a['jobId']}/fail", headers={"Authorization": f"Bearer {t}"},
            json={"code": "x", "message": "Abschnitt 1 ist nicht lesbar", "retryable": False})
    seite = client.get("/verwaltung/warteschlange").text
    assert "Rabenfels" in seite and "Abschnitt 1 ist nicht lesbar" in seite and "Erneut versuchen" in seite
    r = client.post(f"/verwaltung/sessions/{s['id']}/neustart", data={"csrf": admin})
    assert "neu eingereiht" in r.text
    assert client.get(f"{API}/sessions/{s['id']}/status", headers=world["gm"]).json()["state"] == "queued"
    # Übersicht zeigt: wartet, aber kein Worker bereit (der einzige ist pausiert)
    from app.models import Worker
    dbs.get(Worker, t.split(".")[1]).paused = True
    dbs.commit()
    assert "kein Worker ist bereit" in client.get("/verwaltung/").text


def test_einstellungen_wirken_auf_info(client, admin):
    r = client.post("/verwaltung/einstellungen", data={
        "csrf": admin, "server_name": "Taleward", "server_operator": "Drachenhort EV",
        "server_contact": "vorstand@drachenhort.example", "privacy_policy_url": "https://drachenhort.example/ds",
        "min_age": "16", "org_name": "Drachenhort EV"})
    assert "Gespeichert" in r.text
    info = client.get(f"{API}/info").json()
    assert info["name"] == "Taleward" and info["operator"] == "Drachenhort EV"
    assert info["contact"] == "vorstand@drachenhort.example" and info["privacyPolicyUrl"].endswith("/ds")
    r = client.post("/verwaltung/einstellungen", data={"csrf": admin, "server_name": "T", "server_operator": "B",
                                                       "privacy_policy_url": "javascript:alert(1)", "min_age": "16"})
    assert r.status_code == 400


def test_keine_kampagneninhalte(client, world, admin):
    """Die Verwaltung zeigt keine Inhalte: SL-Notiz und Bibel tauchen auf keiner Seite auf."""
    client.post(f"{API}/campaigns/{world['cid']}/entries", headers=world["gm"],
                json={"type": "npc", "name": "GEHEIMER-NSC", "gmNotes": "GEHEIME-NOTIZ"})
    for seite in ("/", "/konten", "/transkription", "/warteschlange", "/einstellungen"):
        text = client.get("/verwaltung" + seite).text
        assert "GEHEIM" not in text, seite


def test_logo_und_symbole(client, admin):
    assert "/verwaltung/static/marke/taleward-lockup-inverse.svg" in client.get("/verwaltung/").text
    for pfad, typ in (("/favicon.ico", "image/"), ("/apple-touch-icon.png", "image/png"),
                      ("/verwaltung/static/marke/taleward-mark.svg", "image/svg+xml")):
        r = client.get(pfad)
        assert r.status_code == 200 and r.headers["content-type"].startswith(typ), pfad
    assert "max-age" in client.get("/verwaltung/static/marke/taleward-mark.svg").headers["cache-control"]
    client.cookies.clear()
    seite = client.get("/verwaltung/anmelden").text
    assert "taleward-lockup-stacked.svg" in seite and "Eure Geschichte, gut verwahrt." in seite
