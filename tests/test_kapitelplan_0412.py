"""Schnittstelle 0.4.12: Kapitelpläne der SL, Unterlagen nachlesen (Text je Seite, Originaldatei)."""
import json
import uuid

import pytest

from tests.test_step2a import API
from tests.test_umzug_048 import _exportieren, _importieren, _zip
from tests.test_step2b import _kleine_teile  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture(autouse=True)
def _sofort(monkeypatch):
    from app import umzug

    monkeypatch.setattr(umzug, "HINTERGRUND", False)


def _eintrag(client, w, name, cid=None, h=None):
    return client.post(f"{API}/campaigns/{cid or w['cid']}/entries", headers=h or w["gm"], json={
        "type": "npc", "name": name, "summary": "x", "visibility": "gm_only"}).json()["id"]


def _unterlage(client, w, name="brief.txt", text="Seite eins.\n\nNoch mehr.", kind="gm", h=None):
    r = client.post(f"{API}/campaigns/{w['cid']}/documents", headers=h or w["gm"],
                    files={"file": (name, text.encode(), "text/plain")}, data={"kind": kind})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _szene(titel, eintraege=(), **mehr):
    return {"id": str(uuid.uuid4()), "title": titel, "entryIds": list(eintraege), **mehr}


def test_plaene_nur_fuer_die_sl(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/plans"
    assert client.get(url, headers=w["gm"]).json() == []
    for h in (w["pl"], w["out"]):  # Spieler und Fremde: 404, nicht einmal die Existenz
        assert client.get(url, headers=h).status_code == 404
        assert client.post(url, headers=h, json={"title": "x"}).status_code == 404
    p = client.post(url, headers=w["gm"], json={"title": "Am Hungerstein", "sessionNumber": 3}).json()
    assert p["state"] == "draft" and p["scenes"] == [] and p["names"] == [] and p["documentIds"] == []
    for h in (w["pl"], w["out"]):
        assert client.get(f"{API}/plans/{p['id']}", headers=h).status_code == 404
        assert client.patch(f"{API}/plans/{p['id']}", headers=h, json={"title": "y"}).status_code == 404
        assert client.delete(f"{API}/plans/{p['id']}", headers=h).status_code == 404
    # Die Kampagne verrät Spielern nichts über Pläne
    assert "plan" not in json.dumps(client.get(f"{API}/campaigns/{w['cid']}", headers=w["pl"]).json()).lower()


def test_plan_anlegen_aendern_konflikt_loeschen(client, world, make_user, login):
    w = world
    url = f"{API}/campaigns/{w['cid']}/plans"
    e1, e2 = _eintrag(client, w, "Bolko"), _eintrag(client, w, "Graue Gilde")
    d1 = _unterlage(client, w)
    sz = _szene("Ankunft", [e1, e2], notes="Nebel", state="open")
    p = client.post(url, headers=w["gm"], json={
        "title": " Kapitel drei ", "sessionNumber": 3, "notes": "Vorbereitung", "scenes": [sz],
        "names": ["Bolko", "Rabenturm", "Bolko", " "], "documentIds": [d1]}).json()
    assert p["title"] == "Kapitel drei" and p["names"] == ["Bolko", "Rabenturm"]
    assert p["scenes"][0]["entryIds"] == [e1, e2] and p["documentIds"] == [d1]
    # fremde Kampagne → 400 invalid_input
    make_user("fremd")
    fremd = login("fremd")
    andere = client.post(f"{API}/campaigns", json={"title": "Andere"}, headers=fremd).json()
    e_fremd = _eintrag(client, w, "Fremder", cid=andere["id"], h=fremd)
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"scenes": [_szene("X", [e_fremd])]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_input"
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"documentIds": ["gibt-es-nicht"]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_input"
    # Gleichzeitig auf zwei Geräten: altes updatedAt → 409 conflict
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"state": "ready", "ifUpdatedAt": p["updatedAt"]})
    assert r.status_code == 200 and r.json()["state"] == "ready"
    neu = r.json()
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"notes": "alt", "ifUpdatedAt": p["updatedAt"]})
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    # Nur Mitgeschicktes ändert sich; Szenen als ganze Liste
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"],
                     json={"scenes": [_szene("Nur noch eine")], "ifUpdatedAt": neu["updatedAt"]})
    q = r.json()
    assert r.status_code == 200 and [s["title"] for s in q["scenes"]] == ["Nur noch eine"]
    assert q["notes"] == "Vorbereitung" and q["names"] == ["Bolko", "Rabenturm"] and q["state"] == "ready"
    # Gelöschte Einträge und Unterlagen fallen heraus
    client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"scenes": [sz]})
    assert client.delete(f"{API}/entries/{e2}", headers=w["gm"]).status_code == 204
    assert client.delete(f"{API}/documents/{d1}", headers=w["gm"]).status_code == 204
    q = client.get(f"{API}/plans/{p['id']}", headers=w["gm"]).json()
    assert q["scenes"][0]["entryIds"] == [e1] and q["documentIds"] == []
    # doppelte Szenen-ID → 400
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"scenes": [sz, sz]})
    assert r.status_code == 400
    assert client.delete(f"{API}/plans/{p['id']}", headers=w["gm"]).status_code == 204
    assert client.get(f"{API}/plans/{p['id']}", headers=w["gm"]).status_code == 404
    assert client.get(f"{API}/entries/{e1}", headers=w["gm"]).status_code == 200  # Eintrag bleibt


def test_reihenfolge_gespielt_und_schreibhilfe(client, world, dbs):
    from app.models import Campaign
    from app.namenshilfe import fuer_kampagne
    from app.routers.plaene import gespielt_markieren

    w = world
    url = f"{API}/campaigns/{w['cid']}/plans"
    for titel, nummer in (("ohne", None), ("fuenf", 5), ("zwei", 2)):
        client.post(url, headers=w["gm"], json={"title": titel, "sessionNumber": nummer,
                                                 "names": [f"Name{titel.capitalize()}"]})
    assert [p["title"] for p in client.get(url, headers=w["gm"]).json()] == ["zwei", "fuenf", "ohne"]
    c = dbs.get(Campaign, w["cid"])
    assert fuer_kampagne(dbs, c, 2)[0] == "NameZwei"
    assert "NameZwei" not in fuer_kampagne(dbs, c, 5) and "NameOhne" not in fuer_kampagne(dbs, c, None)
    gespielt_markieren(dbs, w["cid"], 5)
    dbs.commit()
    zustand = {p["title"]: p["state"] for p in client.get(url, headers=w["gm"]).json()}
    assert zustand == {"zwei": "draft", "fuenf": "played", "ohne": "draft"}


def test_veroeffentlichen_setzt_gespielt(client, world, dbs, tmp_path):
    """Echter Weg: Kapitel mit Nummer 1 veröffentlichen → Plan für Kapitel 1 ist gespielt."""
    from tests.test_step2b import transkribiert, zusammenfassen

    w = world
    p = client.post(f"{API}/campaigns/{w['cid']}/plans", headers=w["gm"],
                    json={"title": "Erster Abend", "sessionNumber": 1, "state": "ready"}).json()
    s = transkribiert(client, w, dbs, tmp_path)
    sid = s["id"]
    sprecher = client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()
    client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"],
               json=[{"speakerId": sprecher[0]["id"], "memberId": w["gm_member"]},
                     {"speakerId": sprecher[1]["id"], "memberId": w["pl_member"]}])
    assert zusammenfassen(dbs)
    r = client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"])
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/plans/{p['id']}", headers=w["gm"]).json()["state"] == "played"


def test_plan_fliesst_nicht_in_die_ki(client, world, dbs, tmp_path):
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe
    from app.models import GameSession
    from tests.test_step2b import transkribiert

    w = world
    client.post(f"{API}/campaigns/{w['cid']}/plans", headers=w["gm"], json={
        "title": "PLAN-GEHEIM", "sessionNumber": 1, "notes": "PLAN-NOTIZ",
        "scenes": [_szene("PLAN-SZENE", notes="PLAN-SZENENNOTIZ")]})
    s = transkribiert(client, w, dbs, tmp_path)
    g = dbs.get(GameSession, s["id"])
    basis = eingabe_bauen(dbs, g)
    alles = json.dumps(recap_eingabe(basis), ensure_ascii=False) + json.dumps(vorschlag_eingabe(dbs, g, basis),
                                                                            ensure_ascii=False)
    assert "PLAN-" not in alles


def test_unterlage_nachlesen(client, world):
    w = world
    d = _unterlage(client, w, "notizen.md", "# Kapitel\n\nDer Wirt heißt Bolko.")
    t = client.get(f"{API}/documents/{d}/text", headers=w["gm"])
    assert t.status_code == 200 and t.json()["pages"] and "Bolko" in t.json()["pages"][0]["text"]
    assert t.json()["pages"][0]["page"] == 1
    f = client.get(f"{API}/documents/{d}/file", headers=w["gm"])
    assert f.status_code == 200 and "Bolko" in f.text
    assert f.headers["cache-control"] == "no-store" and f.headers["content-disposition"].startswith("inline;")
    assert f.headers["x-content-type-options"] == "nosniff" and "sandbox" in f.headers["content-security-policy"]
    assert f.headers["content-type"].startswith("text/plain")  # nie als HTML
    for h in (w["pl"], w["out"]):
        assert client.get(f"{API}/documents/{d}/text", headers=h).status_code == 404
        assert client.get(f"{API}/documents/{d}/file", headers=h).status_code == 404
    # Charakterbogen: die Urheberin und die SL; Text gibt es nicht (wird nie ausgelesen)
    b = _unterlage(client, w, "bogen.txt", "Stärke 14", kind="character_sheet", h=w["pl"])
    assert client.get(f"{API}/documents/{b}/text", headers=w["pl"]).json() == {"pages": []}
    assert client.get(f"{API}/documents/{b}/file", headers=w["pl"]).status_code == 200
    assert client.get(f"{API}/documents/{b}/file", headers=w["gm"]).status_code == 200
    assert client.get(f"{API}/documents/{b}/file", headers=w["out"]).status_code == 404


def test_plaene_beim_umzug(client, world, make_user, login):
    w = world
    e1 = _eintrag(client, w, "Bolko")
    d1 = _unterlage(client, w)
    sz = _szene("Ankunft", [e1])
    client.post(f"{API}/campaigns/{w['cid']}/plans", headers=w["gm"], json={
        "title": "Plan A", "sessionNumber": 2, "scenes": [sz], "names": ["Bolko"], "documentIds": [d1]})
    _, daten = _exportieren(client, w)
    import io
    import zipfile

    inhalt = json.loads(zipfile.ZipFile(io.BytesIO(daten)).read("kampagne.json"))
    plan = inhalt["plans"][0]
    assert plan["title"] == "Plan A" and plan["scenes"][0]["entryKeys"] and plan["documentKeys"]
    assert e1 not in json.dumps(inhalt["plans"]) and d1 not in json.dumps(inhalt["plans"])  # nur Schlüssel
    make_user("eve")
    eve = login("eve")
    st = _importieren(client, eve, daten)
    assert st["state"] == "done", st
    neu = client.get(f"{API}/campaigns/{st['campaignId']}/plans", headers=eve).json()
    assert len(neu) == 1 and neu[0]["title"] == "Plan A" and neu[0]["names"] == ["Bolko"]
    eintraege = {e["id"]: e["name"] for e in client.get(f"{API}/campaigns/{st['campaignId']}/entries",
                                                        headers=eve).json()}
    assert [eintraege[i] for i in neu[0]["scenes"][0]["entryIds"]] == ["Bolko"]
    assert len(neu[0]["documentIds"]) == 1 and neu[0]["documentIds"][0] != d1
    # ältere Umzugsdatei ohne Pläne geht weiter
    alt = _zip({"kampagne.json": json.dumps({"format": "taleward-kampagne/1", "apiVersion": "0.4.8",
                                             "campaign": {"title": "Alt"}})})
    assert _importieren(client, eve, alt)["state"] == "done"
