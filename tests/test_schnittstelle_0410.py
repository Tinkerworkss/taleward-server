"""Schnittstellen 0.4.9 und 0.4.10: Figuren ausgetretener Spieler, 429 bei Fehlversuchen, Kapitel neu schreiben,
Stimmen nach der Bestätigung, Vorschau einer Einladung."""
import pytest

from tests.test_charaktere_047 import _char, _einladen, _pc
from tests.test_pruefung_046 import _zur_pruefung
from tests.test_step2a import API, status
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _hinweise(client, w):
    return client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["gmNotices"]


def _mit_figur(client, w):
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=_char("Tharvok"))
    return _pc(client, w)[0]


# ---------------------------------------------------------------- 0.4.9 Figuren
def test_figur_nach_austritt_als_nsc(client, world):
    w = world
    pc = _mit_figur(client, w)
    url = f"{API}/entries/{pc['id']}/to-npc"
    # solange Ben spielt: nicht möglich; Spieler dürfen gar nicht
    assert client.post(url, headers=w["gm"]).json()["code"] == "holder_active"
    assert client.post(url, headers=w["pl"]).status_code == 403
    assert client.post(f"{API}/entries/gibt-es-nicht/to-npc", headers=w["gm"]).status_code == 404
    # Ben verlässt die Kampagne → Hinweis an die SL
    assert client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"]).status_code == 204
    h = [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]
    assert len(h) == 1 and h[0]["memberId"] == w["pl_member"] and h[0]["entryIds"] == [pc["id"]]
    r = client.post(url, headers=w["gm"])
    assert r.status_code == 200
    e = r.json()
    assert e["type"] == "npc" and e["holderMemberId"] is None and e["formerHolderMemberId"] == w["pl_member"]
    assert e["name"] == "Tharvok" and e["visibility"] == "public"
    assert not [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]  # räumt sich selbst ab
    assert client.post(url, headers=w["gm"]).json()["code"] == "not_a_character"  # schon NSC


def test_figur_einem_anderen_spieler_geben(client, world, make_user, login):
    w = world
    pc = _mit_figur(client, w)
    make_user("dora")
    dora = login("dora")
    c = client.post(f"{API}/campaigns/join", headers=dora, json={"code": _einladen(client, w)}).json()
    dora_id = next(m["id"] for m in c["members"] if m["displayName"] == "Dora")
    url = f"{API}/entries/{pc['id']}/assign"
    assert client.post(url, headers=w["gm"], json={"memberId": dora_id}).json()["code"] == "not_a_character"
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"])
    # nicht an die SL, nicht an Unbekannte, nicht an Ehemalige
    for ziel in (w["gm_member"], "unbekannt", w["pl_member"]):
        assert client.post(url, headers=w["gm"], json={"memberId": ziel}).json()["code"] == "not_a_player"
    assert client.post(url, headers=w["gm"], json={}).status_code == 400
    r = client.post(url, headers=w["gm"], json={"memberId": dora_id})
    assert r.status_code == 200
    e = r.json()
    assert e["type"] == "pc" and e["holderMemberId"] == dora_id and e["formerHolderMemberId"] is None
    assert not [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]
    ich = next(m for m in client.get(f"{API}/campaigns/{w['cid']}", headers=dora).json()["members"]
               if m["id"] == dora_id)
    assert ich["characterName"] == "Tharvok" and ich["characterSummary"].startswith("Tharvok") \
        and ich["characterId"] is None
    # Dora übernimmt die Figur in ihre Sammlung: derselbe Eintrag wird verknüpft, kein zweiter
    neu = _char("Tharvok", summary="Tharvok, jetzt bei Dora.")
    assert client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=dora, json=neu).status_code == 200
    pcs = _pc(client, w)
    assert [p["id"] for p in pcs] == [pc["id"]] and pcs[0]["summary"] == "Tharvok, jetzt bei Dora."
    # Spieler sehen formerHolderMemberId nie
    for x in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=dora).json():
        assert "formerHolderMemberId" not in x


def test_nsc_aus_figur_spaeter_weitergeben_und_doppelte_figur(client, world, make_user, login):
    w = world
    pc = _mit_figur(client, w)
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"])
    client.post(f"{API}/entries/{pc['id']}/to-npc", headers=w["gm"])
    make_user("dora")
    dora = login("dora")
    ch = _char("Wilma")
    c = client.post(f"{API}/campaigns/join", headers=dora, json={"code": _einladen(client, w), "character": ch}).json()
    dora_id = next(m["id"] for m in c["members"] if m["displayName"] == "Dora")
    r = client.post(f"{API}/entries/{pc['id']}/assign", headers=w["gm"], json={"memberId": dora_id})
    assert r.json()["code"] == "member_has_character"  # Dora spielt schon Wilma


def test_kontoloeschung_meldet_figur_und_wiederkehr_raeumt_ab(client, world, make_user, login):
    w = world
    pc = _mit_figur(client, w)
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"])
    assert [n["entryIds"] for n in _hinweise(client, w) if n["code"] == "character_orphaned"] == [[pc["id"]]]
    # Ben kommt mit neuer Einladung zurück → Figur hat ihren Halter wieder, Hinweis weg
    client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": _einladen(client, w)})
    assert not [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]
    # Ben löscht sein Konto → wieder ein Hinweis (nur einer)
    assert client.request("DELETE", f"{API}/me", headers=w["pl"], json={"password": "geheim123"}).status_code == 204
    h = [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]
    assert len(h) == 1 and h[0]["entryIds"] == [pc["id"]]
    assert client.post(f"{API}/entries/{pc['id']}/to-npc", headers=w["gm"]).json()["formerHolderMemberId"] \
        == w["pl_member"]


def test_sl_verlassen_ohne_figurenhinweis(client, world, make_user, login):
    """Hinweise nur für Spieler: eine zweite SL, die geht, löst keinen aus."""
    w = world
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"], json={"role": "gm"})
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"])
    assert not [n for n in _hinweise(client, w) if n["code"] == "character_orphaned"]


def test_429_bei_anmelden_und_beitreten(client, world):
    w = world
    for _ in range(10):
        client.post(f"{API}/auth/login", json={"username": "ben", "password": "falsch"})
    r = client.post(f"{API}/auth/login", json={"username": "ben", "password": "falsch"})
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"
    for _ in range(10):
        client.post(f"{API}/campaigns/join", headers=w["out"], json={"code": "FALSCH-00000000"})
    r = client.post(f"{API}/campaigns/join", headers=w["out"], json={"code": "FALSCH-00000000"})
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"


# ---------------------------------------------------------------- 0.4.10
def test_einladung_vorschau_ohne_anmeldung(client, world):
    w = world
    code = _einladen(client, w)
    r = client.get(f"{API}/invites/{code.lower()}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d == {"campaignTitle": "Rabenfels", "seatCharacterName": None, "expiresAt": d["expiresAt"]}
    assert d["expiresAt"]
    r = client.get(f"{API}/invites/FALSCH-00000000")
    assert r.status_code == 404 and r.json()["code"] == "invite_invalid"
    for _ in range(29):
        client.get(f"{API}/invites/FALSCH-00000000")
    r = client.get(f"{API}/invites/{code}")
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"  # Begrenzung je Adresse wie beim Beitritt


def test_stimmen_nach_bestaetigung_und_kapitel_neu_schreiben(client, world, dbs, tmp_path):
    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    sid = s["id"]
    sp = client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()
    assert [x["assignedMemberId"] for x in sp] == [w["gm_member"], w["pl_member"]]
    # SL bearbeitet den Entwurf und entscheidet einen Vorschlag – neu schreiben verwirft beides
    client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": "VON-HAND-GEAENDERT"})
    vorschlaege = client.get(f"{API}/sessions/{sid}/proposals", headers=w["gm"]).json()
    assert vorschlaege
    client.patch(f"{API}/proposals/{vorschlaege[0]['id']}", headers=w["gm"], json={"decision": "accepted"})
    client.put(f"{API}/sessions/{sid}/gm-note", headers=w["gm"], json={"text": "BLEIBT"})
    assert client.post(f"{API}/sessions/{sid}/resummarize", headers=w["pl"]).status_code == 404
    r = client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"])
    assert r.status_code == 202 and r.json()["state"] == "summarizing"
    assert client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"]).json()["code"] == "wrong_state"
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], sid)["state"] == "awaiting_review"
    assert "VON-HAND-GEAENDERT" not in client.get(f"{API}/sessions/{sid}/recap", headers=w["gm"]).json()["text"]
    assert all(p["decision"] == "open" for p in client.get(f"{API}/sessions/{sid}/proposals", headers=w["gm"]).json())
    assert client.get(f"{API}/sessions/{sid}/gm-note", headers=w["gm"]).json()["text"] == "BLEIBT"
    # Stimmen nachträglich tauschen: Zuordnung ändert sich, Kapitel wird neu geschrieben
    r = client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"],
                   json=[{"speakerId": sp[0]["id"], "memberId": w["pl_member"]},
                         {"speakerId": sp[1]["id"], "memberId": w["gm_member"]}])
    assert r.status_code == 202 and r.json()["state"] == "summarizing"
    sp2 = client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()
    assert [x["assignedMemberId"] for x in sp2] == [w["pl_member"], w["gm_member"]]
    zeilen = client.get(f"{API}/sessions/{sid}/transcript", headers=w["gm"]).json()
    assert {z["memberId"] for z in zeilen} <= {w["pl_member"], w["gm_member"]}
    # während des Schreibens: weder neu schreiben noch Stimmen ändern
    assert client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"], json=[]).json()["code"] == "wrong_state"
    assert zusammenfassen(dbs)
    client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"])
    assert client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"]).json()["code"] == "wrong_state"
    # auch nach der Veröffentlichung abrufbar, mit Zuordnung
    assert client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()[0]["assignedMemberId"] == w["pl_member"]


def test_stimmen_vor_bestaetigung_ohne_zuordnung(client, world, dbs, tmp_path):
    from tests.test_step2b import transkribiert

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    for x in client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json():
        assert "assignedMemberId" not in x


def test_info_meldet_schnittstelle_0411(client):
    assert client.get(f"{API}/info").json()["apiVersion"] == "0.4.15"
