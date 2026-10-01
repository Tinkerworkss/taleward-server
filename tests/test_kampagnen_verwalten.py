"""Schnittstelle 0.4.5: Kampagnen abschließen und löschen, Mitglieder verlassen/entfernen, Rollen, Kapitel verwerfen."""
import pytest

from tests.test_step2a import API, neue_session
from tests.test_step2b import transkribiert

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture()
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def _kampagne(client, w, h=None):
    return client.get(f"{API}/campaigns/{w['cid']}", headers=h or w["gm"])


def test_abschliessen_und_wieder_aufnehmen(client, world):
    w = world
    client.post(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["gm"], json={})
    r = client.patch(f"{API}/campaigns/{w['cid']}", headers=w["pl"], json={"archived": True})
    assert r.status_code == 403
    r = client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"archived": True})
    assert r.status_code == 200 and r.json()["archivedAt"]
    assert client.get(f"{API}/campaigns", headers=w["pl"]).json()[0]["archivedAt"]
    # offene Abstimmung abgebrochen, keine neue, kein neues Kapitel
    assert client.get(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["gm"]).status_code == 404
    r = client.post(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["gm"], json={})
    assert r.status_code == 409 and r.json()["code"] == "campaign_archived"
    r = client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"],
                    json={"playedAt": "2026-09-20T18:00:00Z", "attendees": []})
    assert r.status_code == 409 and r.json()["code"] == "campaign_archived"
    assert client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["pl"]).status_code == 200  # lesen geht

    r = client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"archived": False})
    assert r.json()["archivedAt"] is None
    neue_session(client, w)


def test_kampagne_loeschen(client, world, dbs, tmp_path):
    from app import storage
    from app.models import Campaign, ConsentLog, Upload

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    (up_id,) = [u.id for u in dbs.query(Upload).filter_by(session_id=s["id"])]
    assert storage.upload_dir(up_id).exists()
    r = client.request("DELETE", f"{API}/campaigns/{w['cid']}", headers=w["pl"], json={"confirmTitle": "Rabenfels"})
    assert r.status_code == 403
    r = client.request("DELETE", f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"confirmTitle": "Rabenfel"})
    assert r.status_code == 400 and r.json()["code"] == "confirmation_mismatch" and "Titel" in r.json()["message"]
    r = client.request("DELETE", f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"confirmTitle": " rabenfels "})
    assert r.status_code == 204
    dbs.expire_all()
    assert dbs.get(Campaign, w["cid"]) is None and not storage.upload_dir(up_id).exists()
    assert dbs.query(ConsentLog).filter_by(campaign_id=w["cid"]).count() > 0  # Nachweis bleibt
    assert _kampagne(client, w).status_code == 404 and client.get(f"{API}/campaigns", headers=w["pl"]).json() == []


def test_verlassen_entfernen_und_zurueckkommen(client, world, dbs):
    from app.models import ConsentLog

    w = world
    client.put(f"{API}/campaigns/{w['cid']}/recording-consent", headers=w["pl"], json={"granted": True})
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterBackstory": "Geheim", "characterSummary": "Magierin"})
    # Spieler kann niemand anderen entfernen, die einzige SL kann nicht gehen
    r = client.delete(f"{API}/campaigns/{w['cid']}/members/{w['gm_member']}", headers=w["pl"])
    assert r.status_code == 403
    r = client.delete(f"{API}/campaigns/{w['cid']}/members/{w['gm_member']}", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "last_gm"
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/x", headers=w["gm"]).status_code == 404
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["out"]).status_code == 404

    # Ben geht selbst
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"]).status_code == 204
    assert _kampagne(client, w, w["pl"]).status_code == 404
    assert client.get(f"{API}/campaigns", headers=w["pl"]).json() == []
    k = _kampagne(client, w).json()
    ben = next(m for m in k["members"] if m["id"] == w["pl_member"])
    assert ben["leftAt"] and ben["recordingConsentAt"] is None and ben["characterBackstory"] is None
    assert ben["characterSummary"] == "Magierin" and k["memberCount"] == 1
    assert dbs.query(ConsentLog).filter_by(member_id=w["pl_member"], action="revoked").count() == 1
    r = client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"], json={
        "playedAt": "2026-09-20T18:00:00Z",
        "attendees": [{"memberId": w["pl_member"], "consent": True, "consentSource": "on_site"}]})
    assert r.status_code == 400 and r.json()["code"] == "attendee_unknown"
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"]).status_code == 404

    # Neue Einladung: wieder aktiv, gleiche memberId, als Spieler
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    k = client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": code}).json()
    ben = next(m for m in k["members"] if m["id"] == w["pl_member"])
    assert ben["leftAt"] is None and ben["role"] == "player" and k["memberCount"] == 2

    # Die SL entfernt Ben
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"]).status_code == 204
    assert _kampagne(client, w, w["pl"]).status_code == 404


def test_rollen_wechseln_und_letzte_sl(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/members"
    r = client.patch(f"{url}/{w['gm_member']}", headers=w["pl"], json={"role": "player"})
    assert r.status_code == 403
    r = client.patch(f"{url}/{w['gm_member']}", headers=w["gm"], json={"role": "player"})
    assert r.status_code == 409 and r.json()["code"] == "last_gm"
    assert client.patch(f"{url}/{w['pl_member']}", headers=w["gm"], json={"role": "gm"}).json()["role"] == "gm"
    # Ben ist jetzt SL und sieht Geheimes; Anna darf jetzt gehen
    assert client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"]).status_code == 200
    assert client.delete(f"{url}/{w['gm_member']}", headers=w["gm"]).status_code == 204
    r = client.patch(f"{url}/{w['pl_member']}", headers=w["pl"], json={"role": "player"})
    assert r.status_code == 409  # Anna ist weg – Ben ist die letzte aktive SL


def test_kapitel_verwerfen(client, world, dbs, tmp_path):
    from app import storage
    from app.models import GameSession, Job, Upload

    w = world
    s1 = neue_session(client, w)
    s2 = transkribiert(client, w, dbs, tmp_path)
    s3 = neue_session(client, w)
    (up_id,) = [u.id for u in dbs.query(Upload).filter_by(session_id=s2["id"])]
    assert [s1["number"], s2["number"], s3["number"]] == [1, 2, 3]
    assert client.delete(f"{API}/sessions/{s2['id']}", headers=w["pl"]).status_code == 404
    assert client.delete(f"{API}/sessions/{s2['id']}", headers=w["gm"]).status_code == 204
    dbs.expire_all()
    assert dbs.get(GameSession, s2["id"]) is None and not storage.upload_dir(up_id).exists()
    assert not storage.samples_dir(s2["id"]).exists()
    assert dbs.query(Job).filter_by(session_id=s2["id"]).count() == 0
    assert client.get(f"{API}/sessions/{s3['id']}", headers=w["gm"]).json()["number"] == 2
    assert neue_session(client, w)["number"] == 3

    dbs.get(GameSession, s1["id"]).state = "published"
    dbs.commit()
    r = client.delete(f"{API}/sessions/{s1['id']}", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "session_published"
    assert client.delete(f"{API}/sessions/{s1['id']}", headers=w["pl"]).status_code == 403


def test_sl_uebergabe_an_spielerin(client, world):
    """Übergabe: Ben wird SL, Anna gibt ab und spielt weiter – ohne Berg an Ungelesenem, ohne SL-Wissen."""
    w = world
    url = f"/api/v1/campaigns/{w['cid']}"
    client.post(f"{url}/entries", headers=w["gm"], json={"type": "npc", "name": "Geheim", "visibility": "gm_only"})
    client.post(f"{url}/entries", headers=w["gm"], json={"type": "location", "name": "Markt", "summary": "Laut.",
                                                        "visibility": "public"})
    r = client.patch(f"{url}/members/{w['gm_member']}", headers=w["gm"], json={"role": "player"})
    assert r.status_code == 409 and r.json()["code"] == "last_gm"
    assert client.patch(f"{url}/members/{w['pl_member']}", headers=w["gm"], json={"role": "gm"}).status_code == 200
    assert client.patch(f"{url}/members/{w['gm_member']}", headers=w["gm"], json={"role": "player"}).status_code == 200
    anna = client.get(url, headers=w["gm"]).json()
    assert anna["myRole"] == "player" and anna["unread"]["bible"] == 0 and "gmNotices" not in anna
    namen = {e["name"] for e in client.get(f"{url}/entries", headers=w["gm"]).json()}
    assert namen == {"Markt"}
    assert client.get(url, headers=w["pl"]).json()["myRole"] == "gm"
