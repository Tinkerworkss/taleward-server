"""Schritt 1: Anmeldung, Kampagnen, Sessions, Bibel, Fehlerformat, CORS.
Alle Antworten werden zusätzlich automatisch gegen die YAML geprüft (siehe conftest.py)."""
import pytest

API = "/api/v1"


def set_state(dbs, session_id, state):
    from app.db import utcnow
    from app.models import GameSession

    s = dbs.get(GameSession, session_id)
    s.state = state
    if state == "published":
        s.published_at = utcnow()
    dbs.commit()


def new_session(client, w, consent=True, **extra):
    body = {
        "playedAt": "2026-09-20T18:00:00Z",
        "attendees": [
            {"memberId": w["gm_member"], "consent": consent, "consentSource": "on_site"},
            {"memberId": w["pl_member"], "consent": consent, "consentSource": "on_site"},
        ],
        **extra,
    }
    r = client.post(f"{API}/campaigns/{w['cid']}/sessions", json=body, headers=w["gm"])
    assert r.status_code == 201, r.text
    return r.json()


# ---------- Auth ----------
def test_login_and_me(client, make_user, login):
    make_user("Anna")
    h = login("ANNA")  # Groß-/Kleinschreibung egal
    r = client.get(f"{API}/me", headers=h)
    assert r.json()["username"] == "anna"


def test_login_wrong_password(client, make_user):
    make_user("anna")
    r = client.post(f"{API}/auth/login", json={"username": "anna", "password": "falsch"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"
    r = client.post(f"{API}/auth/login", json={"username": "gibtsnicht", "password": "x"})
    assert r.status_code == 401


def test_requires_token(client):
    assert client.get(f"{API}/me").json()["code"] == "not_authenticated"
    r = client.get(f"{API}/me", headers={"Authorization": "Bearer kaputt"})
    assert r.status_code == 401 and r.json()["code"] == "token_invalid"


def test_password_reset_invalidates_tokens(client, make_user, login, dbs):
    u = make_user("anna")
    h = login("anna")
    u.token_version += 1
    dbs.commit()
    assert client.get(f"{API}/me", headers=h).status_code == 401


def test_geheimnis_wird_selbst_erzeugt(monkeypatch, tmp_path):
    from app import config
    from app.main import check_settings

    monkeypatch.setenv("JWT_SECRET", "bitte-aendern")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "daten"))
    config.get_settings.cache_clear()
    erstes = config.get_settings().jwt_secret
    check_settings()
    assert len(erstes) >= 32 and (tmp_path / "daten" / "geheimnis.txt").stat().st_mode & 0o777 == 0o600
    config.get_settings.cache_clear()
    assert config.get_settings().jwt_secret == erstes  # bleibt nach einem Neustart gleich
    config.get_settings.cache_clear()


# ---------- Kampagnen ----------
def test_campaign_flow(client, world):
    w = world
    r = client.get(f"{API}/campaigns", headers=w["pl"])
    [c] = r.json()
    assert c["myRole"] == "player" and c["myCharacterName"] == "Mira" and c["memberCount"] == 2
    full = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert full["members"][0]["role"] == "gm"


def test_non_member_gets_404(client, world):
    r = client.get(f"{API}/campaigns/{world['cid']}", headers=world["out"])
    assert r.status_code == 404
    r = client.get(f"{API}/campaigns/{world['cid']}/entries", headers=world["out"])
    assert r.status_code == 404


def test_invites(client, world):
    w = world
    assert client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["pl"]).status_code == 403
    r = client.post(f"{API}/campaigns/join", json={"code": "GIBT-0000"}, headers=w["out"])
    assert r.status_code == 404 and r.json()["code"] == "invite_invalid"
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    r = client.post(f"{API}/campaigns/join", json={"code": f" {code.lower()} "}, headers=w["out"])
    assert r.status_code == 200 and len(r.json()["members"]) == 3
    # zweimal beitreten ist harmlos
    r = client.post(f"{API}/campaigns/join", json={"code": code}, headers=w["out"])
    assert len(r.json()["members"]) == 3


def test_expired_invite(client, world, dbs):
    from datetime import timedelta

    from app.db import utcnow
    from app.models import Invite

    code = client.post(f"{API}/campaigns/{world['cid']}/invites", headers=world["gm"]).json()["code"]
    inv = dbs.get(Invite, code)
    inv.expires_at = utcnow() - timedelta(minutes=1)
    dbs.commit()
    assert client.post(f"{API}/campaigns/join", json={"code": code}, headers=world["out"]).status_code == 404


def test_member_patch_rules(client, world):
    w = world
    base = f"{API}/campaigns/{w['cid']}/members"
    r = client.patch(f"{base}/{w['pl_member']}", json={"characterName": "Mira die Kühne"}, headers=w["pl"])
    assert r.status_code == 200 and r.json()["characterName"] == "Mira die Kühne"
    assert client.patch(f"{base}/{w['gm_member']}", json={"characterName": "X"}, headers=w["pl"]).status_code == 403
    assert client.patch(f"{base}/{w['pl_member']}", json={"role": "gm"}, headers=w["pl"]).status_code == 403
    r = client.patch(f"{base}/{w['gm_member']}", json={"role": "player"}, headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "last_gm"
    assert client.patch(f"{base}/{w['pl_member']}", json={"role": "gm"}, headers=w["gm"]).status_code == 200
    assert client.patch(f"{base}/{w['gm_member']}", json={"role": "player"}, headers=w["gm"]).status_code == 200
    assert client.patch(f"{base}/gibtsnicht", json={}, headers=w["gm"]).status_code == 404


# ---------- Sessions ----------
def test_session_create_and_numbering(client, world):
    s1 = new_session(client, world)
    s2 = new_session(client, world, title="  Kapitel Zwei  ")
    assert (s1["number"], s2["number"]) == (1, 2)
    assert s1["state"] == "created" and s1["title"] is None and s2["title"] == "Kapitel Zwei"
    assert all(a["consentAt"] for a in s1["attendees"])


def test_session_validation(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/sessions"
    body = {"playedAt": "2026-09-20T18:00:00Z",
            "attendees": [{"memberId": "fremd", "consent": True, "consentSource": "on_site"}]}
    r = client.post(url, json=body, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "attendee_unknown"
    r = client.post(url, json={"attendees": []}, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "validation_error"
    assert "playedAt" in r.json()["message"]
    assert client.post(url, json=body, headers=w["pl"]).status_code == 403


def test_player_sees_only_published_sessions(client, world, dbs):
    w = world
    s1, s2 = new_session(client, world), new_session(client, world)
    set_state(dbs, s1["id"], "published")
    set_state(dbs, s2["id"], "awaiting_review")
    lst = client.get(f"{API}/campaigns/{w['cid']}/sessions", headers=w["pl"]).json()
    assert [s["id"] for s in lst] == [s1["id"]]
    assert len(client.get(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"]).json()) == 2
    for suffix in ("", "/status", "/recap", "/gm-note", "/transcript", "/speakers", "/proposals"):
        r = client.get(f"{API}/sessions/{s2['id']}{suffix}", headers=w["pl"])
        assert r.status_code == 404, suffix
    for suffix in ("/gm-note", "/transcript", "/speakers"):
        assert client.get(f"{API}/sessions/{s1['id']}{suffix}", headers=w["pl"]).status_code == 403, suffix
    # Vorschläge verraten schon durch ihre Existenz etwas → auch bei veröffentlichten Sessions 404 (Schritt 2b)
    assert client.get(f"{API}/sessions/{s1['id']}/proposals", headers=w["pl"]).status_code == 404
    assert client.get(f"{API}/sessions/{s1['id']}", headers=w["pl"]).status_code == 200
    assert client.get(f"{API}/sessions/{s1['id']}", headers=w["out"]).status_code == 404
    # Zähler: Spieler bekommt keine Hinweise auf offene Prüfungen
    [pc] = client.get(f"{API}/campaigns", headers=w["pl"]).json()
    [gc] = client.get(f"{API}/campaigns", headers=w["gm"]).json()
    assert pc["pendingReviewCount"] == 0 and gc["pendingReviewCount"] == 1
    assert pc["publishedSessionCount"] == 1 and pc["lastPublishedAt"]


def test_session_patch_and_consent(client, world, dbs):
    w = world
    s = new_session(client, world, consent=False)
    r = client.post(f"{API}/sessions/{s['id']}/uploads", json={"source": "table", "files": [{"fileName": "a.m4a", "sizeBytes": 10, "mimeType": "audio/mp4"}]}, headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "consent_missing"
    att = [{"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"},
           {"memberId": w["pl_member"], "consent": True, "consentSource": "on_site"}]
    r = client.patch(f"{API}/sessions/{s['id']}", json={"attendees": att, "title": "Neu"}, headers=w["gm"])
    assert r.status_code == 200 and r.json()["title"] == "Neu"
    assert all(a["consent"] for a in r.json()["attendees"])
    assert client.patch(f"{API}/sessions/{s['id']}", json={"title": "X"}, headers=w["pl"]).status_code == 404
    set_state(dbs, s["id"], "uploading")
    r = client.patch(f"{API}/sessions/{s['id']}", json={"attendees": att}, headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "attendees_locked"


def test_gm_note(client, world):
    w = world
    s = new_session(client, world)
    url = f"{API}/sessions/{s['id']}/gm-note"
    assert client.get(url, headers=w["gm"]).json()["text"] == ""
    assert client.put(url, json={"text": "Hilde ist die Schwester des Fürsten"}, headers=w["gm"]).status_code == 204
    assert client.get(url, headers=w["gm"]).json()["text"].startswith("Hilde")


def test_status_retry_publish(client, world):
    w = world
    s = new_session(client, world)
    st = client.get(f"{API}/sessions/{s['id']}/status", headers=w["gm"]).json()
    assert st["state"] == "created"
    assert client.post(f"{API}/sessions/{s['id']}/retry", headers=w["gm"]).status_code == 409
    assert client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"]).status_code == 409


# ---------- Bibel ----------
def test_entries_spoiler_protection(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/entries"
    r = client.post(url, json={"type": "npc", "name": "Der Graue Fürst", "summary": "Bösewicht"}, headers=w["gm"])
    secret = r.json()
    assert secret["visibility"] == "gm_only"  # Standard ist geheim
    pub = client.post(url, json={"type": "npc", "name": "Hilde Krugmann", "summary": "Wirtin, grüßt den Fürst",
                                 "visibility": "public"}, headers=w["gm"]).json()
    assert [e["name"] for e in client.get(url, headers=w["pl"]).json()] == ["Hilde Krugmann"]
    assert len(client.get(url, headers=w["gm"]).json()) == 2
    # Suche findet für Spieler nichts Geheimes
    assert [e["id"] for e in client.get(url, params={"q": "fürst"}, headers=w["pl"]).json()] == [pub["id"]]
    assert len(client.get(url, params={"q": "FÜRST"}, headers=w["gm"]).json()) == 2
    # Einzelabruf/Ändern/Löschen: geheim → 404, öffentlich → 403
    assert client.get(f"{API}/entries/{secret['id']}", headers=w["pl"]).status_code == 404
    assert client.patch(f"{API}/entries/{secret['id']}", json={"name": "x"}, headers=w["pl"]).status_code == 404
    assert client.delete(f"{API}/entries/{secret['id']}", headers=w["pl"]).status_code == 404
    assert client.patch(f"{API}/entries/{pub['id']}", json={"name": "x"}, headers=w["pl"]).status_code == 403
    assert client.post(url, json={"type": "npc", "name": "x"}, headers=w["pl"]).status_code == 403
    assert client.get(f"{API}/entries/{pub['id']}", headers=w["out"]).status_code == 404


def test_entry_rules(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/entries"
    q = client.post(url, json={"type": "quest", "name": "Siegel", "holderMemberId": w["pl_member"]}, headers=w["gm"]).json()
    assert q["status"] == "active" and q["holderMemberId"] is None
    it = client.post(url, json={"type": "item", "name": "Amulett", "holderMemberId": w["pl_member"],
                                "status": "done"}, headers=w["gm"]).json()
    assert it["holderMemberId"] == w["pl_member"] and it["status"] is None
    r = client.post(url, json={"type": "item", "name": "X", "holderMemberId": "fremd"}, headers=w["gm"])
    assert r.status_code == 400
    assert client.post(url, json={"name": "ohne Typ"}, headers=w["gm"]).status_code == 400
    assert client.get(url, params={"type": "drache"}, headers=w["gm"]).status_code == 400
    assert [e["name"] for e in client.get(url, params={"type": "item"}, headers=w["gm"]).json()] == ["Amulett"]
    # Freigeben ohne öffentlichen Text geht nicht (seit 0.3.5)
    r = client.patch(f"{API}/entries/{q['id']}", json={"visibility": "public"}, headers=w["gm"])
    assert r.status_code == 400 and r.json()["code"] == "validation_error"
    r = client.patch(f"{API}/entries/{q['id']}", json={"status": "done", "visibility": "public",
                                                        "summary": "Das Siegel ist wieder da."}, headers=w["gm"])
    assert r.json()["status"] == "done" and r.json()["visibility"] == "public"
    assert client.delete(f"{API}/entries/{q['id']}", headers=w["gm"]).status_code == 204
    assert client.get(f"{API}/entries/{q['id']}", headers=w["gm"]).status_code == 404


def test_mentions_only_from_published_sessions(client, world, dbs):
    from app.models import EntryMention

    w = world
    s1, s2 = new_session(client, world), new_session(client, world)
    set_state(dbs, s1["id"], "published")
    e = client.post(f"{API}/campaigns/{w['cid']}/entries",
                    json={"type": "npc", "name": "Hilde", "visibility": "public"}, headers=w["gm"]).json()
    dbs.add_all([EntryMention(entry_id=e["id"], session_id=s1["id"], note="eins"),
                 EntryMention(entry_id=e["id"], session_id=s2["id"], note="zwei (geheim)")])
    dbs.commit()
    pl = client.get(f"{API}/entries/{e['id']}", headers=w["pl"]).json()
    gm = client.get(f"{API}/entries/{e['id']}", headers=w["gm"]).json()
    assert [m["note"] for m in pl["mentions"]] == ["eins"] and pl["lastSessionNumber"] == 1
    assert len(gm["mentions"]) == 2 and gm["lastSessionNumber"] == 2
    lst = client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["pl"]).json()
    assert [m["note"] for m in lst[0]["mentions"]] == ["eins"]


# ---------- Stimmprofil (Grundfunktionen) ----------
def test_voice_profile_basics(client, world):
    h = world["pl"]
    assert client.get(f"{API}/me/voice-profile", headers=h).json()["status"] == "none"
    r = client.patch(f"{API}/me/voice-profile", json={"learnFromSessions": True}, headers=h)
    assert r.json()["learnFromSessions"] is True
    assert client.delete(f"{API}/me/voice-profile", headers=h).status_code == 204
    assert client.get(f"{API}/me/voice-profile", headers=h).json()["learnFromSessions"] is False


# ---------- Fehlerformat & CORS ----------
def test_error_format(client, world):
    r = client.post(f"{API}/campaigns", content=b"{kaputt", headers={**world["gm"], "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "validation_error"
    r = client.post(f"{API}/campaigns", json={"title": "   "}, headers=world["gm"])
    assert r.status_code == 400


def test_unknown_route_format(client):
    r = client.get("/gibtsnicht")
    assert r.status_code == 404 and r.json() == {"code": "not_found", "message": "Nicht gefunden."}


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://localhost"])
def test_cors_allowed(client, origin):
    r = client.options(f"{API}/auth/login", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,authorization",
    })
    assert r.headers.get("access-control-allow-origin") == origin


def test_cors_foreign_origin(client):
    r = client.get(f"{API}/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ---------- Selbsttest der Vertragsprüfung ----------
def test_contract_checker_catches_mistakes():
    from tests.contract import CONTRACT

    with pytest.raises(AssertionError, match="displayName"):
        CONTRACT.check("GET", "/api/v1/me", 200, "application/json", b'{"id": "x", "username": "a"}')
    with pytest.raises(AssertionError, match="Statuscode 422"):
        CONTRACT.check("GET", "/api/v1/me", 422, "application/json", b'{}')
    with pytest.raises(AssertionError, match="nicht in der YAML"):
        CONTRACT.check("GET", "/api/v1/gibtsnicht", 200, "application/json", b'{}')
    with pytest.raises(AssertionError):
        CONTRACT.check("GET", "/api/v1/sessions/abc/status", 200, "application/json",
                       b'{"state": "fertig", "progress": null, "queuePosition": null, "message": null, '
                       b'"updatedAt": "2026-01-01T00:00:00Z"}')
    with pytest.raises(AssertionError):
        CONTRACT.check("GET", "/api/v1/me", 200, "application/json",
                       b'{"id": "x", "username": "a", "displayName": null}')
