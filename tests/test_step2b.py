"""Schritt 2b: Stimmen bestätigen, Zusammenfassung (Attrappe), Recap und Vorschläge prüfen, veröffentlichen."""
import pytest

from tests.test_step2a import API, audio_abschnitte, hochladen, neue_session, worker_starten, status, worker_token

pytestmark = pytest.mark.usefixtures("_kleine_teile")


@pytest.fixture()
def _kleine_teile(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(64 * 1024))


def zusammenfassen(dbs) -> bool:
    from app.zusammenfassung import einen_auftrag

    dbs.expire_all()
    return einen_auftrag(dbs)


def transkribiert(client, w, dbs, tmp_path, source="table"):
    """Session bis „Stimmen zuordnen“ (Tisch) bzw. „Zusammenfassen“ (Discord) bringen – echter Weg über Upload."""
    s = neue_session(client, w)
    if source == "table":
        hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (45,)))
    else:
        hochladen(client, w["gm"], s["id"], audio_abschnitte(tmp_path, (20, 20)), "discord",
                  [w["gm_member"], w["pl_member"]])
    assert worker_starten(client, worker_token(dbs), tmp_path).einen_auftrag()
    return s


def bibel(client, w):
    """Ein öffentlicher und ein geheimer Eintrag, mit Markern, die nie ins Sprachmodell dürfen."""
    oeff = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Rabenfels", "summary": "Kleine Stadt am Finsterwald.",
        "visibility": "public", "gmNotes": "MARKER-GMNOTES-OEFFENTLICH"}).json()
    geheim = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Der Graue Fürst", "summary": "MARKER-GEHEIMER-TEXT", "visibility": "gm_only",
        "gmNotes": "MARKER-GMNOTES-GEHEIM"}).json()
    return oeff, geheim


# ---------------------------------------------------------------- der ganze Weg
def test_vom_upload_bis_zur_veroeffentlichung(client, world, dbs, tmp_path):
    w = world
    oeff, geheim = bibel(client, w)
    s = transkribiert(client, w, dbs, tmp_path)
    sid = s["id"]
    sprecher = client.get(f"{API}/sessions/{sid}/speakers", headers=w["gm"]).json()
    assert len(sprecher) == 2

    # Stimmen bestätigen: Stimme 1 = SL, Stimme 2 bleibt beim (leeren) Vorschlag → nicht genannt
    r = client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"],
                   json=[{"speakerId": sprecher[0]["id"], "memberId": w["gm_member"]},
                         {"speakerId": sprecher[1]["id"], "memberId": w["pl_member"]}])
    assert r.status_code == 202 and r.json()["state"] == "summarizing"
    # Hörproben sind weg, Stimmabdrücke auch
    assert client.get(f"{API}/sessions/{sid}/speakers/{sprecher[0]['id']}/sample", headers=w["gm"]).status_code == 410
    from app.models import Speaker
    dbs.expire_all()
    assert all(sp.embedding is None and sp.sample_path is None
               for sp in dbs.query(Speaker).filter_by(session_id=sid))
    zeilen = client.get(f"{API}/sessions/{sid}/transcript", headers=w["gm"]).json()
    assert {z["memberId"] for z in zeilen} == {w["gm_member"], w["pl_member"]}

    assert zusammenfassen(dbs)
    assert status(client, w["gm"], sid)["state"] == "awaiting_review"
    assert not zusammenfassen(dbs)  # nichts mehr zu tun

    # Recap: SL ja, Spieler nicht (Session unveröffentlicht)
    recap = client.get(f"{API}/sessions/{sid}/recap", headers=w["gm"]).json()
    assert recap["text"].startswith("Platzhalter") and recap["publishedAt"] is None and len(recap["openThreads"]) == 2
    assert client.get(f"{API}/sessions/{sid}/recap", headers=w["pl"]).status_code == 404
    r = client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"],
                   json={"title": "Nebel über Rabenfels", "text": "Es war einmal …", "openThreads": [" Wer? ", ""]})
    assert r.status_code == 200 and r.json()["openThreads"] == ["Wer?"]

    # Vorschläge: neu (öffentlich), neu (geheim, Scherz?), ergänzen, aufdecken
    vs = {(v["action"], v["entryType"]): v for v in
          client.get(f"{API}/sessions/{sid}/proposals", headers=w["gm"]).json()}
    assert set(vs) == {("create", "npc"), ("create", "location"), ("update", "location"), ("reveal", "npc")}
    reveal = vs[("reveal", "npc")]
    assert reveal["targetEntryId"] == geheim["id"]
    assert "MARKER-GEHEIMER-TEXT" in reveal["gmNotes"] and "MARKER-GMNOTES-GEHEIM" in reveal["gmNotes"]
    assert vs[("create", "location")]["flags"] == ["joke_suspected", "low_confidence"]
    assert vs[("create", "npc")]["evidence"][0]["quote"]
    assert client.get(f"{API}/sessions/{sid}/proposals", headers=w["pl"]).status_code == 404

    def patch(v, **body):
        r = client.patch(f"{API}/proposals/{v['id']}", headers=w["gm"], json=body)
        assert r.status_code == 200, r.text
        return r.json()

    neu = patch(vs[("create", "npc")], decision="accepted", title="Der Kellermeister",
                hiddenFromMemberIds=[w["pl_member"]])
    assert neu["hiddenFromMemberIds"] == [w["pl_member"]]
    patch(reveal, decision="accepted", detail="Eine Stimme sprach vom Fürsten.")
    patch(vs[("update", "location")], decision="accepted", detail="Ein Gang führt zur Burg.")
    patch(vs[("create", "location")], decision="rejected")

    r = client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"])
    assert r.status_code == 200 and r.json()["state"] == "published"
    assert r.json()["title"] == "Nebel über Rabenfels"  # Session ohne Titel übernimmt den des Recaps
    # Spieler: Recap sichtbar, Zähler „neu“
    r = client.get(f"{API}/sessions/{sid}/recap", headers=w["pl"])
    assert r.status_code == 200 and r.json()["title"] == "Nebel über Rabenfels" and r.json()["publishedAt"]
    [camp] = client.get(f"{API}/campaigns", headers=w["pl"]).json()
    assert camp["unread"]["recaps"] == 1
    # Bibel aus Spielersicht
    eintraege = {e["name"]: e for e in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["pl"]).json()}
    assert "Der Kellermeister" not in eintraege  # vor Ben verborgen
    assert eintraege["Der Graue Fürst"]["summary"] == "Eine Stimme sprach vom Fürsten."
    assert "gmNotes" not in eintraege["Der Graue Fürst"]
    assert eintraege["Rabenfels"]["summary"] == "Kleine Stadt am Finsterwald.\n\nEin Gang führt zur Burg."
    assert "Platzhalter-Ort" not in eintraege
    # SL sieht alles, der geheime Teil blieb beim Aufdecken erhalten
    gm_sicht = {e["name"]: e for e in client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"]).json()}
    assert "MARKER-GMNOTES-GEHEIM" in gm_sicht["Der Graue Fürst"]["gmNotes"]
    assert gm_sicht["Der Kellermeister"]["hiddenFromMemberIds"] == [w["pl_member"]]
    assert "Platzhalter-Ort" not in gm_sicht
    furst = client.get(f"{API}/entries/{geheim['id']}", headers=w["pl"]).json()
    assert furst["mentions"][0]["sessionNumber"] == s["number"]

    # Danach ist nichts mehr änderbar
    assert client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"]).json()["code"] == "already_published"
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": "x"}).status_code == 409
    assert client.patch(f"{API}/proposals/{reveal['id']}", headers=w["gm"],
                        json={"decision": "rejected"}).status_code == 409
    # offene Vorschläge gelten nach dem Veröffentlichen als verworfen, Spieler sehen Vorschläge nie
    assert client.patch(f"{API}/proposals/{reveal['id']}", headers=w["pl"], json={}).status_code == 404
    usage = client.get(f"{API}/campaigns/{w['cid']}/usage", headers=w["gm"]).json()
    assert usage["sessions"] == 1


def test_offene_vorschlaege_werden_verworfen(client, world, dbs, tmp_path):
    w = world
    bibel(client, w)
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    zusammenfassen(dbs)
    vorher = len(client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"]).json())
    assert client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"]).status_code == 200
    assert len(client.get(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"]).json()) == vorher
    assert {v["decision"] for v in client.get(f"{API}/sessions/{s['id']}/proposals", headers=w["gm"]).json()} \
        == {"rejected"}
    # Titel der Session kommt aus dem Recap, wenn sie keinen hatte
    assert client.get(f"{API}/sessions/{s['id']}", headers=w["pl"]).json()["title"].startswith("Kapitel")


# ---------------------------------------------------------------- Spoilerschutz der Eingabe
def test_sprachmodell_bekommt_keine_geheimnisse(client, world, dbs, tmp_path):
    from app.models import GameSession
    from app.zusammenfassung import eingabe_bauen

    w = world
    bibel(client, w)
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Mira", "characterBackstory": "MARKER-HINTERGRUND"})
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/gm-note", headers=w["gm"], json={"text": "MARKER-SL-NOTIZ"})
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    dbs.expire_all()
    eingabe = eingabe_bauen(dbs, dbs.get(GameSession, s["id"]))
    roh = eingabe.als_json()
    assert "MARKER" not in roh
    assert [g.name for g in eingabe.geheim] == ["Der Graue Fürst"]  # nur der Name
    assert "Mira" in roh and "Kleine Stadt am Finsterwald." in roh


# ---------------------------------------------------------------- Prüfungen
def test_stimmen_pruefungen(client, world, dbs, tmp_path):
    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    url = f"{API}/sessions/{s['id']}/speakers"
    sp = client.get(url, headers=w["gm"]).json()
    assert client.put(url, headers=w["pl"], json=[]).status_code == 404  # Spieler: Session unsichtbar
    assert client.put(url, headers=w["gm"], json=[{"speakerId": "gibtsnicht"}]).json()["code"] == "speaker_unknown"
    assert client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"], "memberId": "fremd"}]).json()["code"] \
        == "member_unknown"
    assert client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"]}, {"speakerId": sp[0]["id"]}]).json()[
        "code"] == "speaker_duplicate"
    # null = Gast/ignorieren
    assert client.put(url, headers=w["gm"], json=[{"speakerId": sp[0]["id"], "memberId": None}]).status_code == 202
    r = client.put(url, headers=w["gm"], json=[])
    assert r.status_code == 409 and r.json()["code"] == "wrong_state"  # 0.4.10 (vorher invalid_state)


def test_vorschlag_und_recap_pruefungen(client, world, dbs, tmp_path):
    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    sid = s["id"]
    # vor der Zusammenfassung: kein Recap, Veröffentlichen nicht möglich
    assert client.get(f"{API}/sessions/{sid}/recap", headers=w["gm"]).status_code == 404
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": "x"}).status_code == 404
    assert client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"]).json()["code"] == "not_ready"
    client.put(f"{API}/sessions/{sid}/speakers", headers=w["gm"], json=[])
    zusammenfassen(dbs)
    v = client.get(f"{API}/sessions/{sid}/proposals", headers=w["gm"]).json()[0]
    url = f"{API}/proposals/{v['id']}"
    assert client.patch(url, headers=w["gm"], json={"title": "  "}).status_code == 400
    r = client.patch(url, headers=w["gm"], json={"detail": "", "decision": "accepted"})
    assert r.status_code == 400 and "öffentlichen Text" in r.json()["message"]
    assert client.patch(url, headers=w["gm"], json={"hiddenFromMemberIds": ["fremd"]}).json()["code"] \
        == "hidden_member_unknown"
    # geheim gemacht → verborgen-Liste spielt keine Rolle, leerer Text erlaubt
    r = client.patch(url, headers=w["gm"], json={"visibility": "gm_only", "detail": "", "decision": "accepted",
                                                 "hiddenFromMemberIds": [w["pl_member"]], "gmNotes": ""})
    assert r.status_code == 200 and r.json()["hiddenFromMemberIds"] == [] and r.json()["gmNotes"] is None
    assert client.patch(url, headers=w["out"], json={}).status_code == 404
    assert client.patch(f"{API}/proposals/gibtsnicht", headers=w["gm"], json={}).status_code == 404
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": " "}).status_code == 400
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["pl"], json={"text": "x"}).status_code == 404


# ---------------------------------------------------------------- Discord, Fehler, Neustart, abgeschaltet
def test_discord_geht_direkt_zur_zusammenfassung(client, world, dbs, tmp_path):
    w = world
    s = transkribiert(client, w, dbs, tmp_path, "discord")
    assert status(client, w["gm"], s["id"])["state"] == "summarizing"
    sp = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    assert client.get(f"{API}/sessions/{s['id']}/speakers/{sp[0]['id']}/sample", headers=w["gm"]).status_code == 410
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"
    assert "Mira" in client.get(f"{API}/sessions/{s['id']}/recap", headers=w["gm"]).json()["text"]


def test_zusammenfassung_scheitert_und_neustart(client, world, dbs, tmp_path):
    from app import zusammenfassung

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])

    def kaputt(_db, _s):
        raise RuntimeError("Sprachmodell nicht erreichbar")

    original = zusammenfassung.zusammenfasser
    zusammenfassung.zusammenfasser = lambda _db: kaputt
    try:
        for _ in range(3):
            assert zusammenfassen(dbs)
    finally:
        zusammenfassung.zusammenfasser = original
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "failed" and "Zusammenfassung ist fehlgeschlagen" in st["message"]
    assert "Sprachmodell nicht erreichbar" in st["message"]
    r = client.post(f"{API}/sessions/{s['id']}/retry", headers=w["gm"])
    assert r.status_code == 202 and r.json()["state"] == "summarizing"
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"


def test_zusammenfassung_abgeschaltet(client, world, dbs, tmp_path, monkeypatch):
    from app import config

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    monkeypatch.setenv("SUMMARIZER", "aus")
    config.get_settings.cache_clear()
    assert not zusammenfassen(dbs)
    assert "abgeschaltet" in status(client, w["gm"], s["id"])["message"]


# ---------------------------------------------------------------- Demodaten
def test_demodaten_kapitel_zwei_veroeffentlichen(client, login, dbs):
    from typer.testing import CliRunner

    from app.cli import app

    assert CliRunner().invoke(app, ["demo-data"]).exit_code == 0
    sl, sp = login("sl", "chronik-demo"), login("spieler", "chronik-demo")
    [camp] = client.get(f"{API}/campaigns", headers=sl).json()
    k1, k2 = client.get(f"{API}/campaigns/{camp['id']}/sessions", headers=sl).json()
    assert client.get(f"{API}/sessions/{k1['id']}/recap", headers=sp).status_code == 200
    vs = client.get(f"{API}/sessions/{k2['id']}/proposals", headers=sl).json()
    assert [v["action"] for v in vs] == ["create", "reveal", "update", "create"]
    for v in vs[:3]:
        client.patch(f"{API}/proposals/{v['id']}", headers=sl, json={"decision": "accepted"})
    assert client.post(f"{API}/sessions/{k2['id']}/publish", headers=sl).status_code == 200
    namen = {e["name"] for e in client.get(f"{API}/campaigns/{camp['id']}/entries", headers=sp).json()}
    assert {"Der Kellermeister", "Der Graue Fürst"} <= namen and "Goldene Gummiente" not in namen


def test_teilweise_verborgene_eintraege_nicht_im_recap(client, world, dbs, tmp_path):
    """0.3.6: Den Recap lesen alle – ein vor einzelnen Spielern verborgener Eintrag zählt dort wie gm_only."""
    from app.models import GameSession
    from app.zusammenfassung import eingabe_bauen

    w = world
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Die Verschwörerin", "summary": "MARKER-NUR-FUER-EINIGE", "visibility": "public",
        "hiddenFromMemberIds": [w["pl_member"]]})
    s = transkribiert(client, w, dbs, tmp_path)
    dbs.expire_all()
    eingabe = eingabe_bauen(dbs, dbs.get(GameSession, s["id"]))
    assert "MARKER-NUR-FUER-EINIGE" not in eingabe.als_json()
    assert [g.name for g in eingabe.geheim] == ["Die Verschwörerin"]


def test_update_vorschlag_verbirgt_eintrag(client, world, dbs, tmp_path):
    w = world
    oeff, _ = bibel(client, w)
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    zusammenfassen(dbs)
    upd = [v for v in client.get(f"{API}/sessions/{s['id']}/proposals", headers=w["gm"]).json()
           if v["action"] == "update"][0]
    client.patch(f"{API}/proposals/{upd['id']}", headers=w["gm"],
                 json={"decision": "accepted", "hiddenFromMemberIds": [w["pl_member"]]})
    client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"])
    assert client.get(f"{API}/entries/{oeff['id']}", headers=w["gm"]).json()["hiddenFromMemberIds"] == [w["pl_member"]]
    assert client.get(f"{API}/entries/{oeff['id']}", headers=w["pl"]).status_code == 404
