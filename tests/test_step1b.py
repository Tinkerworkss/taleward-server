"""Schritt 1b: Grundlagen aus Schnittstelle 0.3.3. Alle Antworten werden zusätzlich gegen die YAML geprüft."""
import sqlite3
import uuid

import jwt

API = "/api/v1"
EN = {"Accept-Language": "en-US,en;q=0.9"}


def members_by_name(client, w, headers):
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=headers).json()
    return {m["displayName"]: m for m in c["members"]}


def session_body(*attendees):
    return {"playedAt": "2026-09-20T18:00:00Z", "attendees": list(attendees)}


# ---------- Server & Anmeldung ----------
def test_info_ist_oeffentlich(client):
    r = client.get(f"{API}/info")
    assert r.status_code == 200
    i = r.json()
    assert i["apiVersion"] == "0.4.7" and i["externalTranscription"] is None and i["authMethods"] == ["password"] and i["registration"] == "invite_only"


def test_registrierung_geschlossen(client, dbs):
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "registrierung", "closed")
    dbs.commit()
    assert client.get(f"{API}/info").json()["registration"] == "closed"
    r = client.post(f"{API}/auth/register", json={"inviteCode": "X", "username": "a", "displayName": "A",
                                                   "password": "12345678", "acceptPrivacy": True, "ageConfirmed": True})
    assert r.status_code == 403 and r.json()["code"] == "registration_closed"


def test_token_ist_an_server_gebunden(client, make_user, login, monkeypatch):
    u = make_user("anna")
    h = login("anna")
    assert client.get(f"{API}/me", headers=h).status_code == 200
    secret = "test-secret-" + "x" * 40
    fremd = jwt.encode({"sub": u.id, "ver": 0, "aud": "anderer-server", "exp": 9999999999}, secret, "HS256")
    ohne = jwt.encode({"sub": u.id, "ver": 0, "exp": 9999999999}, secret, "HS256")
    for t in (fremd, ohne):
        r = client.get(f"{API}/me", headers={"Authorization": f"Bearer {t}"})
        assert r.status_code == 401 and r.json()["code"] == "token_invalid"


def test_fehlermeldungen_zweisprachig(client, make_user):
    make_user("anna")
    body = {"username": "anna", "password": "falsch"}
    de = client.post(f"{API}/auth/login", json=body).json()["message"]
    en = client.post(f"{API}/auth/login", json=body, headers=EN).json()["message"]
    assert de == "Benutzername oder Passwort ist falsch." and en == "Wrong username or password."
    r = client.post(f"{API}/auth/login", json={"username": "anna"}, headers={"Accept-Language": "en"})
    assert r.json()["message"] == "Required field missing: password."


def test_organisationen(client, world):
    orgs = client.get(f"{API}/organizations", headers=world["pl"]).json()
    assert len(orgs) == 1 and orgs[0]["myRole"] == "member"
    c = client.get(f"{API}/campaigns/{world['cid']}", headers=world["gm"]).json()
    assert c["organization"]["id"] == orgs[0]["id"]
    r = client.post(f"{API}/campaigns", json={"title": "X", "organizationId": "fremd"}, headers=world["gm"])
    assert r.status_code == 400 and r.json()["code"] == "organization_invalid"


# ---------- Kampagne ----------
def test_kampagne_anlegen_und_bearbeiten(client, world):
    w = world
    r = client.post(f"{API}/campaigns", json={"title": "Household", "language": "en", "system": "other",
                                              "systemName": "Household"}, headers=w["gm"])
    c = r.json()
    assert r.status_code == 201 and c["language"] == "en" and c["systemName"] == "Household"
    assert c["coverPreset"] is not None and c["worldInfo"] is None
    url = f"{API}/campaigns/{w['cid']}"
    r = client.patch(url, json={"worldInfo": "Die Welt ist klein.", "system": "dsa", "coverPreset": "castle"},
                     headers=w["gm"])
    assert r.status_code == 200
    c = r.json()
    assert c["worldInfo"] == "Die Welt ist klein." and c["system"] == "dsa" and c["coverPreset"] == "castle"
    assert client.patch(url, json={"system": None}, headers=w["gm"]).json()["system"] is None
    assert client.patch(url, json={"title": "  "}, headers=w["gm"]).status_code == 400
    assert client.patch(url, json={"system": "gurps"}, headers=w["gm"]).status_code == 400
    assert client.patch(url, json={"worldInfo": "x"}, headers=w["pl"]).status_code == 403
    assert client.get(url, headers=w["pl"]).json()["worldInfo"] == "Die Welt ist klein."


# ---------- Charakter ----------
def test_charakter_hintergrund_nur_selbst_und_sl(client, world, make_user, login):
    w = world
    base = f"{API}/campaigns/{w['cid']}/members"
    r = client.patch(f"{base}/{w['pl_member']}", headers=w["pl"],
                     json={"characterSummary": "Elfe mit Bogen", "characterBackstory": "Geheime Vergangenheit"})
    assert r.status_code == 200 and r.json()["characterBackstory"] == "Geheime Vergangenheit"
    # SL darf fremde Beschreibung nicht ändern
    r = client.patch(f"{base}/{w['pl_member']}", json={"characterSummary": "x"}, headers=w["gm"])
    assert r.status_code == 403
    # zweiter Spieler sieht die Kurzbeschreibung, aber nicht den Hintergrund
    make_user("dora")
    dora = login("dora")
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    client.post(f"{API}/campaigns/join", json={"code": code}, headers=dora)
    ben_fuer_dora = members_by_name(client, w, dora)["Ben"]
    assert ben_fuer_dora["characterSummary"] == "Elfe mit Bogen"
    assert "characterBackstory" not in ben_fuer_dora
    assert members_by_name(client, w, w["gm"])["Ben"]["characterBackstory"] == "Geheime Vergangenheit"
    assert members_by_name(client, w, w["pl"])["Ben"]["characterBackstory"] == "Geheime Vergangenheit"
    # Leerer Text löscht
    r = client.patch(f"{base}/{w['pl_member']}", json={"characterBackstory": ""}, headers=w["pl"])
    assert r.json()["characterBackstory"] is None


def test_zu_lange_kurzbeschreibung(client, world):
    r = client.patch(f"{API}/campaigns/{world['cid']}/members/{world['pl_member']}",
                     json={"characterSummary": "x" * 1001}, headers=world["pl"])
    assert r.status_code == 400


# ---------- Einwilligung ----------
def test_stehende_zustimmung_und_protokoll(client, world, dbs):
    from app.models import ConsentLog

    w = world
    url = f"{API}/campaigns/{w['cid']}/recording-consent"
    r = client.put(url, json={"granted": True}, headers=w["pl"])
    assert r.status_code == 200 and r.json()["recordingConsentAt"]
    r = client.put(url, json={"granted": False}, headers=w["pl"])
    assert r.json()["recordingConsentAt"] is None
    log = [x.action for x in dbs.query(ConsentLog).order_by(ConsentLog.at)]
    assert log == ["granted", "revoked"]


def test_anwesende_regeln(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/sessions"
    gm_vor_ort = {"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"}
    ben_app = {"memberId": w["pl_member"], "consent": True, "consentSource": "app"}
    # Ben hat in der App noch nicht zugestimmt → keine Zustimmung „stellvertretend“
    r = client.post(url, json=session_body(gm_vor_ort, ben_app), headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "consent_missing" and "Ben" in r.json()["message"]
    client.put(f"{API}/campaigns/{w['cid']}/recording-consent", json={"granted": True}, headers=w["pl"])
    gast = {"guestName": "Tante Erna", "consent": True, "consentSource": "on_site"}
    r = client.post(url, json=session_body(gm_vor_ort, ben_app, gast), headers=w["gm"])
    assert r.status_code == 201
    att = r.json()["attendees"]
    assert att[1]["consentSource"] == "app" and att[1]["consentAt"]
    assert att[2] == {"guestName": "Tante Erna", "consent": True, "consentSource": "on_site",
                      "consentAt": att[2]["consentAt"]}
    for falsch, code in [
        ({"guestName": "Gast", "consent": True, "consentSource": "app"}, "invalid_attendee"),
        ({"memberId": w["gm_member"], "guestName": "X", "consent": True, "consentSource": "on_site"}, "invalid_attendee"),
        ({"consent": True, "consentSource": "on_site"}, "invalid_attendee"),
    ]:
        r = client.post(url, json=session_body(falsch), headers=w["gm"])
        assert r.status_code == 400 and r.json()["code"] == code


def test_widerruf_blockiert_upload(client, world):
    w = world
    client.put(f"{API}/campaigns/{w['cid']}/recording-consent", json={"granted": True}, headers=w["pl"])
    s = client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"], json=session_body(
        {"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"},
        {"memberId": w["pl_member"], "consent": True, "consentSource": "app"},
    )).json()
    up = f"{API}/sessions/{s['id']}/uploads"
    client.put(f"{API}/campaigns/{w['cid']}/recording-consent", json={"granted": False}, headers=w["pl"])
    r = client.post(up, json={"source": "table", "files": [{"fileName": "a.m4a", "sizeBytes": 10, "mimeType": "audio/mp4"}]}, headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "consent_revoked"


# ---------- Bibel: geheimer Teil ----------
def test_gm_notes_nur_fuer_sl(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/entries"
    e = client.post(url, headers=w["gm"], json={"type": "npc", "name": "Hilde", "summary": "Wirtin",
                                                "visibility": "public", "gmNotes": "Spioniert für den Fürsten"}).json()
    assert e["gmNotes"] == "Spioniert für den Fürsten"
    fuer_spieler = client.get(f"{API}/entries/{e['id']}", headers=w["pl"]).json()
    assert "gmNotes" not in fuer_spieler
    assert all("gmNotes" not in x for x in client.get(url, headers=w["pl"]).json())
    assert client.get(url, params={"q": "Fürsten"}, headers=w["pl"]).json() == []
    assert len(client.get(url, params={"q": "Fürsten"}, headers=w["gm"]).json()) == 1


# ---------- Ungelesen ----------
def test_ungelesen_bibel_und_chronik(client, world, dbs):
    from app.db import utcnow
    from app.models import GameSession

    w = world
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"],
                json={"type": "location", "name": "Taverne", "visibility": "public"})
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"],
                json={"type": "npc", "name": "Geheim", "visibility": "gm_only"})
    s = client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"], json=session_body(
        {"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"})).json()
    sess = dbs.get(GameSession, s["id"])
    sess.state, sess.published_at = "published", utcnow()
    dbs.commit()

    def unread(h):
        return client.get(f"{API}/campaigns", headers=h).json()[0]["unread"]

    assert unread(w["pl"]) == {"recaps": 1, "comments": 0, "bible": 1}
    assert unread(w["gm"]) == {"recaps": 0, "comments": 0, "bible": 0}
    assert client.post(f"{API}/campaigns/{w['cid']}/seen", json={"area": "bible"}, headers=w["pl"]).status_code == 204
    client.post(f"{API}/campaigns/{w['cid']}/seen", json={"area": "chronicle"}, headers=w["pl"])
    assert unread(w["pl"]) == {"recaps": 0, "comments": 0, "bible": 0}
    assert client.post(f"{API}/sessions/{s['id']}/seen", headers=w["pl"]).status_code == 204


# ---------- Verbrauch, Einladungsseite, spätere Funktionen ----------
def test_verbrauch(client, world):
    url = f"{API}/campaigns/{world['cid']}/usage"
    assert client.get(url, headers=world["gm"]).json()["sessions"] == 0
    assert client.get(url, params={"month": "2026-09"}, headers=world["gm"]).json()["month"] == "2026-09"
    assert client.get(url, params={"month": "09/2026"}, headers=world["gm"]).status_code == 400
    assert client.get(url, headers=world["pl"]).status_code == 403


def test_einladungsseite(client, world):
    code = client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]
    r = client.get(f"/einladung/{code.lower()}")
    assert r.status_code == 200 and code in r.text and "In Taleward öffnen" in r.text
    assert "Rabenfels" not in r.text  # öffentlich: kein Kampagnentitel
    assert 'src="data:image/svg+xml' in r.text  # QR-Code mit dem Link, ohne Skript
    assert client.get("/einladung/GIBTS-NICHT").status_code == 404


def test_spaetere_funktionen_antworten_verstaendlich(client, world, dbs):
    w = world
    s = client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"], json=session_body(
        {"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"})).json()
    assert client.get(f"{API}/sessions/{s['id']}/comments", headers=w["gm"]).json() == []
    r = client.get(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["pl"])
    assert r.status_code == 404 and r.json()["code"] == "no_date_poll"
    assert client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["gm"]).json() == []
    assert client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"]).json() == []  # 0.4.7: Spieler sehen nur eigene Charakterbögen
    assert client.put(f"{API}/uploads/x/files/y/chunks/0", content=b"x", headers=w["gm"]).status_code == 404
    # Spoilerschutz auch hier: Kommentare unveröffentlichter Kapitel für Spieler unsichtbar
    assert client.get(f"{API}/sessions/{s['id']}/comments", headers=w["pl"]).status_code == 404


# ---------- Migration einer Datenbank aus Schritt 1 ----------
def test_migration_aus_schritt1(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    from app import config, db
    from app.security import hash_password

    dbfile = tmp_path / "chronik.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "y" * 40)
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()
    db.reset_engine()
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbfile}")
    command.upgrade(cfg, "c05348d6cac5")  # Stand Schritt 1

    con = sqlite3.connect(dbfile)
    uid, cid, mid, sid = (str(uuid.uuid4()) for _ in range(4))
    con.execute("INSERT INTO users VALUES (?, 'alt', 'Alter Hase', ?, 0, '2026-09-01 10:00:00')",
                (uid, hash_password("altespasswort")))
    con.execute("INSERT INTO campaigns VALUES (?, 'Alte Kampagne', '', '2026-09-01 10:00:00')", (cid,))
    con.execute("INSERT INTO members VALUES (?, ?, ?, 'gm', NULL, '2026-09-01 10:00:00')", (mid, cid, uid))
    con.execute("INSERT INTO sessions (id, campaign_id, number, played_at, state, state_updated_at, created_at) "
                "VALUES (?, ?, 1, '2026-09-02 18:00:00', 'created', '2026-09-02 18:00:00', '2026-09-02 18:00:00')",
                (sid, cid))
    con.execute("INSERT INTO attendees VALUES (?, ?, 0, 1, '2026-09-02 18:00:00')", (sid, mid))
    con.commit()
    con.close()

    from app.main import create_app

    with TestClient(create_app()) as c:  # Start führt die Migration aus
        r = c.post(f"{API}/auth/login", json={"username": "alt", "password": "altespasswort"})
        assert r.status_code == 200, r.text
        h = {"Authorization": f"Bearer {r.json()['accessToken']}"}
        camp = c.get(f"{API}/campaigns/{cid}", headers=h).json()
        assert camp["organization"]["name"] == "Rollenspielverein" and camp["language"] == "de"
        s = c.get(f"{API}/sessions/{sid}", headers=h).json()
        assert s["attendees"] == [{"memberId": mid, "consent": True, "consentSource": "on_site",
                                   "consentAt": "2026-09-02T18:00:00Z"}]
    db.reset_engine()
    config.get_settings.cache_clear()


# ---------- 0.3.4 – 0.3.7 ----------
def test_externe_transkription_schalter(client, world):
    url = f"{API}/campaigns/{world['cid']}"
    assert client.get(url, headers=world["gm"]).json()["allowExternalTranscription"] is False
    r = client.patch(url, json={"allowExternalTranscription": True}, headers=world["gm"])
    assert r.json()["allowExternalTranscription"] is True
    assert client.patch(url, json={"allowExternalTranscription": False}, headers=world["pl"]).status_code == 403
    s = client.post(f"{url}/sessions", headers=world["gm"], json=session_body(
        {"memberId": world["gm_member"], "consent": True, "consentSource": "on_site"})).json()
    assert s["transcriptionEngine"] is None


def test_wer_weiss_was(client, world, make_user, login):
    """Öffentlicher Eintrag, vor einem Spieler verborgen: für ihn unsichtbar – überall."""
    w = world
    make_user("dora")
    dora = login("dora")
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    dora_id = client.post(f"{API}/campaigns/join", json={"code": code}, headers=dora).json()["members"]
    dora_id = next(m["id"] for m in dora_id if m["displayName"] == "Dora")
    url = f"{API}/campaigns/{w['cid']}/entries"
    e = client.post(url, headers=w["gm"], json={"type": "npc", "name": "Der Einsiedler", "summary": "Lebt im Moor",
                                                "visibility": "public", "hiddenFromMemberIds": [w["pl_member"]]}).json()
    assert e["hiddenFromMemberIds"] == [w["pl_member"]]
    # Ben (verborgen) sieht nichts, Dora sieht den Eintrag – aber nie die Liste der Verborgenen
    assert client.get(url, headers=w["pl"]).json() == []
    assert client.get(url, params={"q": "Moor"}, headers=w["pl"]).json() == []
    assert client.get(f"{API}/entries/{e['id']}", headers=w["pl"]).status_code == 404
    fuer_dora = client.get(f"{API}/entries/{e['id']}", headers=dora).json()
    assert fuer_dora["name"] == "Der Einsiedler" and "hiddenFromMemberIds" not in fuer_dora
    unread = {c["id"]: c["unread"]["bible"] for c in client.get(f"{API}/campaigns", headers=w["pl"]).json()}
    assert unread[w["cid"]] == 0
    # Aufheben: jetzt sieht Ben ihn auch
    r = client.patch(f"{API}/entries/{e['id']}", json={"hiddenFromMemberIds": []}, headers=w["gm"])
    assert r.json()["hiddenFromMemberIds"] == []
    assert len(client.get(url, headers=w["pl"]).json()) == 1
    # Unbekannte Mitglieder werden abgelehnt
    r = client.patch(f"{API}/entries/{e['id']}", json={"hiddenFromMemberIds": ["fremd"]}, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "hidden_member_unknown"
