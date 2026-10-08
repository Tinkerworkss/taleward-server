"""Schnittstelle 0.4.13: Links an Kampagne, Einträgen und Szenen; Notizen am SL-Schirm (tableNotes)."""
import io
import json
import uuid
import zipfile

import pytest

from tests.test_step2a import API
from tests.test_umzug_048 import _exportieren, _importieren
from tests.test_step2b import _kleine_teile  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture(autouse=True)
def _sofort(monkeypatch):
    from app import umzug

    monkeypatch.setattr(umzug, "HINTERGRUND", False)


def _link(label, url, shared=False):
    return {"id": str(uuid.uuid4()), "label": label, "url": url, "shared": shared}


def test_kampagnen_links_sl_und_spieler(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}"
    assert client.get(url, headers=w["gm"]).json()["links"] == []
    tisch, karte = _link(" Spieltisch ", "https://vtt.example/raum/7", True), _link("Karte", "http://karte.example/x")
    r = client.patch(url, headers=w["gm"], json={"links": [tisch, karte]})
    assert r.status_code == 200, r.text
    assert [x["label"] for x in r.json()["links"]] == ["Spieltisch", "Karte"]  # Name getrimmt
    assert r.json()["links"][1]["shared"] is False
    # Spieler: nur geteilte
    sp = client.get(url, headers=w["pl"]).json()["links"]
    assert [x["url"] for x in sp] == ["https://vtt.example/raum/7"]
    assert "karte.example" not in json.dumps(client.get(f"{API}/campaigns", headers=w["pl"]).json())
    # Spieler dürfen nicht ändern
    assert client.patch(url, headers=w["pl"], json={"links": []}).status_code == 403
    # ältere App: PATCH ohne links lässt die Links stehen
    assert len(client.patch(url, headers=w["gm"], json={"title": "Neu"}).json()["links"]) == 2
    # Liste ganz ersetzen
    assert client.patch(url, headers=w["gm"], json={"links": [karte]}).json()["links"][0]["label"] == "Karte"
    assert client.get(url, headers=w["pl"]).json()["links"] == []
    assert client.patch(url, headers=w["gm"], json={"links": []}).json()["links"] == []


@pytest.mark.parametrize("adresse", [
    "javascript:alert(1)", "ftp://x.example/a", "https://nutzer:geheim@x.example/", "https://nutzer@x.example/",
    "https://", "https://x.example/a b", "x.example", "file:///etc/passwd", "data:text/html,hi",
])
def test_ungueltige_adressen(client, world, adresse):
    w = world
    r = client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"links": [_link("x", adresse)]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_input", r.text


def test_grenzen_und_doppelte_ids(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}"
    zuviel = [_link(f"L{i}", "https://x.example") for i in range(21)]
    assert client.patch(url, headers=w["gm"], json={"links": zuviel}).status_code == 400
    assert client.patch(url, headers=w["gm"], json={"links": [_link("x" * 61, "https://x.example")]}).status_code == 400
    assert client.patch(url, headers=w["gm"], json={"links": [_link("   ", "https://x.example")]}).status_code == 400
    a = _link("a", "https://x.example")
    r = client.patch(url, headers=w["gm"], json={"links": [a, {**a, "label": "b"}]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_input"


def test_eintrags_links(client, world):
    w = world
    offen, geheim = _link("Stadtplan", "https://karte.example/stadt", True), _link("SL-Notiz", "https://notiz.example")
    r = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Hafen", "summary": "Laut.", "visibility": "public", "links": [offen, geheim]})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert len(r.json()["links"]) == 2
    sp = client.get(f"{API}/entries/{eid}", headers=w["pl"]).json()
    assert [x["label"] for x in sp["links"]] == ["Stadtplan"]
    alle = json.dumps(client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["pl"]).json())
    assert "notiz.example" not in alle and "karte.example" in alle
    # mehr als 3 → 400
    zuviel = [_link(f"L{i}", "https://x.example") for i in range(4)]
    assert client.patch(f"{API}/entries/{eid}", headers=w["gm"], json={"links": zuviel}).status_code == 400
    assert client.patch(f"{API}/entries/{eid}", headers=w["gm"],
                        json={"links": [_link("x", "javascript:alert(1)")]}).status_code == 400
    # PATCH ohne links lässt sie stehen
    assert len(client.patch(f"{API}/entries/{eid}", headers=w["gm"], json={"summary": "Leise."}).json()["links"]) == 2


def test_links_an_geheimem_eintrag_erst_nach_dem_aufdecken(client, world):
    w = world
    geteilt = _link("Bild", "https://bild.example/1", True)
    eid = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Mira", "summary": "Wirtin.", "visibility": "gm_only", "links": [geteilt]}).json()["id"]
    assert client.get(f"{API}/entries/{eid}", headers=w["pl"]).status_code == 404
    assert "bild.example" not in json.dumps(client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["pl"]).json())
    r = client.patch(f"{API}/entries/{eid}", headers=w["gm"], json={"visibility": "public"})
    assert r.json()["links"] == [{**geteilt, "id": geteilt["id"].lower()}]  # Aufdecken ändert die Links nicht
    assert client.get(f"{API}/entries/{eid}", headers=w["pl"]).json()["links"][0]["url"] == "https://bild.example/1"


def test_szenen_links_und_tischnotizen(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/plans"
    sz = {"id": str(uuid.uuid4()), "title": "Ankunft", "links": [_link("Musik", "https://musik.example/a", True)]}
    p = client.post(url, headers=w["gm"], json={"title": "Plan", "scenes": [sz], "tableNotes": " Bolko hat 3 Silber "})
    assert p.status_code == 201, p.text
    p = p.json()
    assert p["tableNotes"] == "Bolko hat 3 Silber" and p["scenes"][0]["links"][0]["label"] == "Musik"
    # dieselbe ifUpdatedAt-Regel
    r = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"],
                     json={"tableNotes": "neu", "ifUpdatedAt": p["updatedAt"]})
    assert r.status_code == 200 and r.json()["tableNotes"] == "neu"
    r2 = client.patch(f"{API}/plans/{p['id']}", headers=w["gm"],
                      json={"tableNotes": "alt", "ifUpdatedAt": p["updatedAt"]})
    assert r2.status_code == 409
    # ohne tableNotes bleibt die Notiz; null leert sie
    assert client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"title": "P2"}).json()["tableNotes"] == "neu"
    assert client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"tableNotes": None}).json()["tableNotes"] is None
    # ungültige Links an Szenen
    schlecht = {**sz, "links": [_link("x", "ftp://x.example")]}
    assert client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"scenes": [schlecht]}).status_code == 400
    viele = {**sz, "links": [_link(f"L{i}", "https://x.example") for i in range(4)]}
    assert client.patch(f"{API}/plans/{p['id']}", headers=w["gm"], json={"scenes": [viele]}).status_code == 400
    # Spieler: weiter nichts
    assert client.get(f"{API}/plans/{p['id']}", headers=w["pl"]).status_code == 404


def test_links_und_notizen_fliessen_nicht_in_die_ki(client, world, dbs, tmp_path):
    from app import namenshilfe
    from app.models import Campaign, GameSession
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe
    from tests.test_step2b import transkribiert

    w = world
    client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={
        "links": [_link("LINKKAMPAGNE", "https://kampagne-link.example/", True)]})
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Bolko", "summary": "Wirt.", "gmNotes": "geheim", "visibility": "public",
        "links": [_link("LINKEINTRAG", "https://eintrag-link.example/", True),
                  _link("LINKSL", "https://sl-link.example/")]})
    client.post(f"{API}/campaigns/{w['cid']}/plans", headers=w["gm"], json={
        "title": "Plan", "sessionNumber": 1, "tableNotes": "TISCHNOTIZ",
        "scenes": [{"id": str(uuid.uuid4()), "title": "S", "links": [_link("LINKSZENE", "https://szene-link.example/")]}]})
    s = transkribiert(client, w, dbs, tmp_path)
    g = dbs.get(GameSession, s["id"])
    basis = eingabe_bauen(dbs, g)
    c = dbs.get(Campaign, w["cid"])
    alles = (json.dumps(recap_eingabe(basis), ensure_ascii=False)
             + json.dumps(vorschlag_eingabe(dbs, g, basis), ensure_ascii=False)
             + json.dumps(namenshilfe.fuer_kampagne(dbs, c, 1), ensure_ascii=False)
             + json.dumps(namenshilfe.anzeige(dbs, c), ensure_ascii=False))
    assert "Bolko" in alles  # der Eintrag selbst ist dabei
    for wort in ("LINK", "link.example", "TISCHNOTIZ"):
        assert wort not in alles


def test_links_und_notizen_beim_umzug(client, world, make_user, login):
    w = world
    client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={
        "links": [_link("Tisch", "https://vtt.example/", True)]})
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Bolko", "summary": "Wirt.", "visibility": "public",
        "links": [_link("Bild", "https://bild.example/")]})
    client.post(f"{API}/campaigns/{w['cid']}/plans", headers=w["gm"], json={
        "title": "Plan", "tableNotes": "Notiz", "scenes": [{"id": str(uuid.uuid4()), "title": "S",
                                                           "links": [_link("Musik", "https://musik.example/")]}]})
    _, daten = _exportieren(client, w)
    inhalt = json.loads(zipfile.ZipFile(io.BytesIO(daten)).read("kampagne.json"))
    # Ungültiges in der Datei fällt still heraus, statt den Umzug abzubrechen
    inhalt["campaign"]["links"].append({"id": str(uuid.uuid4()), "label": "böse", "url": "javascript:x"})
    puffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(daten)) as alt, zipfile.ZipFile(puffer, "w") as neu:
        for n in alt.namelist():
            neu.writestr(n, json.dumps(inhalt) if n == "kampagne.json" else alt.read(n))
    make_user("eve")
    eve = login("eve")
    st = _importieren(client, eve, puffer.getvalue())
    assert st["state"] == "done", st
    cid = st["campaignId"]
    assert [x["label"] for x in client.get(f"{API}/campaigns/{cid}", headers=eve).json()["links"]] == ["Tisch"]
    e = client.get(f"{API}/campaigns/{cid}/entries", headers=eve).json()
    assert [x["label"] for x in next(x for x in e if x["name"] == "Bolko")["links"]] == ["Bild"]
    p = client.get(f"{API}/campaigns/{cid}/plans", headers=eve).json()[0]
    assert p["tableNotes"] == "Notiz" and p["scenes"][0]["links"][0]["label"] == "Musik"
