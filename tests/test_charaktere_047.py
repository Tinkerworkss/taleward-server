"""Schnittstelle 0.4.7: Charaktere aus der Sammlung der App, mitgebrachte Welt, Chronik, Hinweise an die SL, Gäste."""
import uuid

import pytest

from tests.test_pruefung_046 import _zur_pruefung
from tests.test_step2a import API
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _char(name="Tharvok", version=1, cid=None, **mehr):
    return {"id": cid or str(uuid.uuid4()), "version": version, "name": name, "status": "active",
            "summary": f"{name}, Söldner aus dem Norden.", "backstory": "GEHEIMER-HINTERGRUND", **mehr}


def _einladen(client, w):
    return client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]


def _pc(client, w, h=None):
    return [e for e in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=h or w["gm"]).json()
            if e["type"] == "pc"]


def test_charakter_setzen_aktualisieren_loesen(client, world):
    w = world
    ch = _char()
    url = f"{API}/campaigns/{w['cid']}/members/me/character"
    m = client.put(url, headers=w["pl"], json=ch).json()
    assert m["characterId"] == ch["id"] and m["characterVersion"] == 1 and m["characterName"] == "Tharvok"
    assert m["characterStatus"] == "active" and m["characterBackstory"] == "GEHEIMER-HINTERGRUND"
    # pc-Eintrag: öffentlich, Halter = das Mitglied, für Spieler sichtbar
    pc = _pc(client, w, w["pl"])
    assert len(pc) == 1 and pc[0]["holderMemberId"] == w["pl_member"] and pc[0]["visibility"] == "public"
    # gleiche oder ältere Fassung → 409 mit details.serverVersion
    r = client.put(url, headers=w["pl"], json=ch)
    assert r.status_code == 409 and r.json()["code"] == "character_version_stale"
    assert r.json()["details"] == {"serverVersion": 1}
    # anderer Charakter → character_mismatch
    assert client.put(url, headers=w["pl"], json=_char("Anderer")).json()["code"] == "character_mismatch"
    # höhere Fassung: Member und pc-Eintrag ziehen mit
    m = client.put(url, headers=w["pl"], json={**ch, "version": 2, "name": "Tharvok der Graue",
                                               "status": "retired"}).json()
    assert m["characterVersion"] == 2 and m["characterStatus"] == "retired"
    assert _pc(client, w)[0]["name"] == "Tharvok der Graue"
    # die SL kann pc-Einträge nicht anlegen und nicht umwidmen
    r = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"],
                    json={"type": "pc", "name": "X", "visibility": "public", "summary": "x"})
    assert r.status_code == 400
    assert client.patch(f"{API}/entries/{_pc(client, w)[0]['id']}", headers=w["gm"],
                        json={"type": "npc"}).status_code == 400
    # lösen: pc-Eintrag bleibt ohne Halter, danach neuer Charakter möglich
    assert client.delete(url, headers=w["pl"]).status_code == 204
    assert client.delete(url, headers=w["pl"]).json()["code"] == "no_character"
    assert _pc(client, w)[0]["holderMemberId"] is None
    m = client.put(url, headers=w["pl"], json=_char("Litha")).json()
    assert m["characterName"] == "Litha" and len(_pc(client, w)) == 2
    # Außenstehende: 404
    assert client.put(url, headers=w["out"], json=_char()).status_code == 404


def test_beitritt_mit_charakter_und_neuzugang(client, world, make_user, login):
    w = world
    # Ein öffentlicher Eintrag, der vor Ben verborgen ist – wer neu kommt, soll ihn auch nicht sehen
    e = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Die Verräterin", "summary": "Hat Ben verraten.", "visibility": "public",
        "hiddenFromMemberIds": [w["pl_member"]]}).json()
    offen = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Marktplatz", "summary": "Laut.", "visibility": "public"}).json()
    make_user("dora")
    dora = login("dora")
    ch = _char("Dorin", 3)
    c = client.post(f"{API}/campaigns/join", headers=dora, json={"code": _einladen(client, w), "characterName": "x",
                                                                 "character": ch}).json()
    ich = next(m for m in c["members"] if m["displayName"] == "Dora")
    assert ich["characterName"] == "Dorin" and ich["characterId"] == ch["id"] and ich["characterVersion"] == 3
    assert "gmNotices" not in c  # Spieler sehen keine Hinweise
    sichtbar = {x["id"] for x in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=dora).json()}
    assert offen["id"] in sichtbar and e["id"] not in sichtbar
    # Die SL bekommt einen Hinweis und erledigt ihn
    def hinweise():  # ohne member_joined (0.4.14, eigener Test)
        return [n for n in client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["gmNotices"]
                if n["code"] != "member_joined"]

    assert [(n["code"], n["memberId"], n["entryIds"]) for n in hinweise()] == [
        ("hidden_entries_for_newcomer", ich["id"], [e["id"]])]
    nid = hinweise()[0]["id"]
    assert client.delete(f"{API}/campaigns/{w['cid']}/gm-notices/{nid}", headers=w["pl"]).status_code == 404
    assert client.delete(f"{API}/campaigns/{w['cid']}/gm-notices/{nid}", headers=w["gm"]).status_code == 204
    assert hinweise() == []
    # Derselbe Charakter kann nicht zweimal in einer Kampagne mitspielen
    r = client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json={**ch, "version": 9})
    assert r.status_code == 409 and r.json()["code"] == "character_in_campaign"
    # Wiederbeitritt mit gleichem Stand: nichts passiert
    c = client.post(f"{API}/campaigns/join", headers=dora, json={"code": _einladen(client, w), "character": ch})
    assert c.status_code == 200


def test_mitgebrachte_welt(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/members/me/world"
    ort = {"id": str(uuid.uuid4()), "version": 1, "type": "location", "name": "Nebelhain",
           "summary": "Heimatdorf im Moor.", "secret": False}
    feind = {"id": str(uuid.uuid4()), "version": 1, "type": "npc", "name": "Bruder Kael",
             "summary": "Sucht Tharvok.", "secret": True}
    assert client.post(url, headers=w["pl"], json={"entries": [ort]}).json()["code"] == "no_character"
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=_char())
    assert client.post(url, headers=w["pl"], json={"entries": []}).json()["code"] == "world_too_many"
    aus = client.post(url, headers=w["pl"], json={"entries": [ort, feind]}).json()["entries"]
    assert [a["state"] for a in aus] == ["pending", "pending"] and aus[0]["entryId"] is None
    # Spieler: keine Prüfliste, kein Zähler
    assert client.get(f"{API}/campaigns/{w['cid']}/character-proposals", headers=w["pl"]).status_code == 404
    assert client.get(f"{API}/campaigns", headers=w["pl"]).json()[0]["openCharacterProposals"] == 0
    assert client.get(f"{API}/campaigns", headers=w["gm"]).json()[0]["openCharacterProposals"] == 2
    liste = client.get(f"{API}/campaigns/{w['cid']}/character-proposals", headers=w["gm"]).json()
    assert {p["source"] for p in liste} == {"character"} and liste[0]["submittedByMemberId"] == w["pl_member"]
    p_ort, p_feind = liste
    assert p_feind["hiddenFromMemberIds"] == []  # nur die SL und Ben in der Kampagne – niemand sonst
    # Annehmen wirkt sofort
    r = client.patch(f"{API}/proposals/{p_ort['id']}", headers=w["gm"], json={"decision": "accepted"}).json()
    assert r["decision"] == "accepted"
    assert client.patch(f"{API}/proposals/{p_ort['id']}", headers=w["gm"],
                        json={"decision": "rejected"}).status_code == 409
    client.patch(f"{API}/proposals/{p_feind['id']}", headers=w["gm"], json={"decision": "rejected"})
    eintraege = {e["name"]: e for e in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"]).json()}
    assert eintraege["Nebelhain"]["originEntryId"] == ort["id"] and eintraege["Nebelhain"]["originVersion"] == 1
    assert "Bruder Kael" not in eintraege
    # Herkunft: SL und Urheberin ja, andere Spieler nein (hier: Ben ist Urheber)
    assert client.get(f"{API}/entries/{eintraege['Nebelhain']['id']}", headers=w["pl"]).json()["originVersion"] == 1
    stand = {s["id"]: s for s in client.get(url, headers=w["pl"]).json()["entries"]}
    assert stand[ort["id"]]["state"] == "accepted" and stand[ort["id"]]["serverVersion"] == 1
    assert stand[feind["id"]]["state"] == "rejected"
    # Gleiche Fassung erneut: unverändert bzw. weiter abgelehnt; neue Fassung → update-Vorschlag
    aus = client.post(url, headers=w["pl"], json={"entries": [ort, feind]}).json()["entries"]
    assert [a["state"] for a in aus] == ["unchanged", "rejected"]
    aus = client.post(url, headers=w["pl"], json={"entries": [{**ort, "version": 2, "summary": "Abgebrannt."}]}).json()
    pid = aus["entries"][0]["proposalId"]
    p = next(x for x in client.get(f"{API}/campaigns/{w['cid']}/character-proposals", headers=w["gm"]).json()
             if x["id"] == pid)
    assert p["action"] == "update" and p["targetEntryId"] == eintraege["Nebelhain"]["id"]
    client.patch(f"{API}/proposals/{pid}", headers=w["gm"], json={"decision": "accepted"})
    e = client.get(f"{API}/entries/{eintraege['Nebelhain']['id']}", headers=w["gm"]).json()
    assert e["summary"] == "Abgebrannt." and e["originVersion"] == 2


def test_geheime_welt_vor_anderen_verborgen(client, world, make_user, login):
    w = world
    make_user("dora")
    dora = login("dora")
    client.post(f"{API}/campaigns/join", headers=dora, json={"code": _einladen(client, w), "characterName": "Dorin"})
    dora_id = next(m["id"] for m in client.get(f"{API}/campaigns/{w['cid']}", headers=dora).json()["members"]
                   if m["displayName"] == "Dora")
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=_char())
    geheim = {"id": str(uuid.uuid4()), "version": 1, "type": "npc", "name": "Bruder Kael",
              "summary": "Sucht Tharvok.", "secret": True}
    client.post(f"{API}/campaigns/{w['cid']}/members/me/world", headers=w["pl"], json={"entries": [geheim]})
    p = client.get(f"{API}/campaigns/{w['cid']}/character-proposals", headers=w["gm"]).json()[0]
    assert p["hiddenFromMemberIds"] == [dora_id]
    client.patch(f"{API}/proposals/{p['id']}", headers=w["gm"], json={"decision": "accepted"})
    assert "Bruder Kael" in {e["name"] for e in client.get(f"{API}/campaigns/{w['cid']}/entries",
                                                           headers=w["pl"]).json()}
    assert "Bruder Kael" not in {e["name"] for e in client.get(f"{API}/campaigns/{w['cid']}/entries",
                                                               headers=dora).json()}


def test_chronik_und_export(client, world, dbs, tmp_path):
    w = world
    ch = _char("Mira")
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=ch)
    s = _zur_pruefung(client, w, dbs, tmp_path)
    client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"])
    client.post(f"{API}/sessions/{s['id']}/comments", headers=w["pl"], json={"text": "Schöne Runde!"})
    url = f"{API}/campaigns/{w['cid']}/members/me/chronicle"
    assert client.get(url, headers=w["out"]).status_code == 404
    c = client.get(url, headers=w["pl"]).json()
    assert c["member"]["characterId"] == ch["id"] and c["campaign"]["title"] == "Rabenfels"
    assert c["sessions"][0]["attended"] is True and c["sessions"][0]["recap"]["text"]
    assert c["comments"][0]["text"] == "Schöne Runde!"
    assert any(m["entryType"] == "pc" for m in c["mentions"])
    roh = client.get(url, headers=w["pl"]).text
    assert "GEHEIMER-HINTERGRUND" not in roh and "Anna" not in roh  # keine Namen anderer Personen
    # auch nach dem Verlassen
    client.delete(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"])
    assert client.get(url, headers=w["pl"]).status_code == 200
    export = client.get(f"{API}/me/export", headers=w["pl"]).json()
    assert export["characters"][0]["characterId"] == ch["id"]
    assert export["characters"][0]["campaigns"][0]["leftAt"] is not None


def test_charakterbogen(client, world):
    w = world
    dateien = {"file": ("bogen.txt", "Stärke 14\n\nGeschick 12".encode(), "text/plain")}
    r = client.post(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"], files=dateien,
                    data={"kind": "character_sheet"})
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "done" and d["uploadedByMemberId"] == w["pl_member"] and d["proposalCount"] == 0
    # andere Arten bleiben der SL vorbehalten
    assert client.post(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"], files=dateien,
                       data={"kind": "handout"}).status_code == 403
    assert [x["id"] for x in client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"]).json()] == [d["id"]]
    assert client.get(f"{API}/documents/{d['id']}", headers=w["pl"]).status_code == 200
    assert client.get(f"{API}/documents/{d['id']}", headers=w["out"]).status_code == 404
    assert len(client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["gm"]).json()) == 1
    assert client.delete(f"{API}/documents/{d['id']}", headers=w["pl"]).status_code == 204


def test_gast_stimme_benennen(client, world, dbs, tmp_path):
    from app.models import Attendee, GameSession
    from tests.test_step2b import transkribiert

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    sitzung = dbs.get(GameSession, s["id"])
    sitzung.attendees.append(Attendee(guest_name="Mira", position=9, consent=True, consent_source="on_site"))
    dbs.commit()
    sp = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    url = f"{API}/sessions/{s['id']}/speakers"
    r = client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"], "memberId": w["gm_member"],
                                                "guestName": "Mira"}])
    assert r.status_code == 400 and r.json()["code"] == "speaker_member_and_guest"
    r = client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"], "guestName": "Unbekannt"}])
    assert r.json()["code"] == "guest_unknown"
    r = client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"], "memberId": w["gm_member"]},
                                               {"speakerId": sp[1]["id"], "memberId": None, "guestName": "mira"}])
    assert r.status_code == 202
    neu = {x["id"]: x for x in client.get(url, headers=w["gm"]).json()}
    assert neu[sp[1]["id"]]["assignedGuestName"] == "Mira" and neu[sp[0]["id"]]["assignedGuestName"] is None
    from app.zusammenfassung import eingabe_bauen

    dbs.expire_all()
    zeilen = eingabe_bauen(dbs, dbs.get(GameSession, s["id"])).transkript
    assert any(z.sprecher == "Mira (Gast)" for z in zeilen)
