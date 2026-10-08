"""Schnittstelle 0.4.8: Kampagnen-Umzug – Zustimmung, Export, Import, offene Plätze."""
import hashlib
import io
import json
import uuid
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_step2a import API

GEHEIM = "MARKER-TRANSKRIPT-GEHEIM"
PRUEF = "MARKER-PRUEFTEIL"


@pytest.fixture(autouse=True)
def _sofort(monkeypatch):
    from app import umzug

    monkeypatch.setattr(umzug, "HINTERGRUND", False)  # Export und Import laufen in der Anfrage


def _char(name, version=1, cid=None):
    return {"id": cid or str(uuid.uuid4()), "version": version, "name": name, "status": "active",
            "summary": f"{name}, Kurzbeschreibung.", "backstory": f"HINTERGRUND-{name.upper()}"}


def _png(farbe=(200, 40, 40)) -> bytes:
    from PIL import Image

    puffer = io.BytesIO()
    Image.new("RGB", (300, 200), farbe).save(puffer, "PNG")
    return puffer.getvalue()


def _kapitel(dbs, w, nummer, veroeffentlicht=True, gaeste=()):
    """Kapitel direkt in der Datenbank – mit Transkript und Prüfteil, die nie in die Datei dürfen."""
    from app.db import utcnow
    from app.models import Attendee, GameSession, GmNote, Recap, TranscriptSegment

    s = GameSession(campaign_id=w["cid"], number=nummer, title=f"Kapitel {nummer}", played_at=utcnow(),
                    state="published" if veroeffentlicht else "awaiting_review",
                    published_at=utcnow() if veroeffentlicht else None)
    s.attendees = [Attendee(member_id=w["gm_member"], position=0, consent=True),
                   Attendee(member_id=w["pl_member"], position=1, consent=True)]
    s.attendees += [Attendee(guest_name=g, position=2 + i, consent=True) for i, g in enumerate(gaeste)]
    dbs.add(s)
    dbs.flush()
    dbs.add(Recap(session_id=s.id, title=f"Recap {nummer}", text=f"Die Gruppe zog weiter ({nummer}).",
                  open_threads='["Wer ist der Fremde?"]', review=json.dumps({"x": PRUEF})))
    dbs.add(GmNote(session_id=s.id, text=f"SL-NOTIZ-{nummer}"))
    dbs.add(TranscriptSegment(session_id=s.id, position=0, start=0, end=1, text=GEHEIM))
    dbs.commit()
    return s.id


def _warten_export(client, w, xid):
    x = client.get(f"{API}/campaigns/{w['cid']}/exports/{xid}", headers=w["gm"]).json()
    assert x["state"] == "ready", x
    return x


def _aufbauen(client, w, dbs, make_user, login):
    """Ben stimmt zu (Charakter, Porträt, Bogen), Dora nicht. Kapitel, Kommentare, Bibel, Unterlagen."""
    ben_char = _char("Tharvok")
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=ben_char)
    r = client.put(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}/portrait", headers={
        **w["pl"], "Content-Type": "image/png"}, content=_png())
    assert r.status_code == 200
    make_user("dora")
    dora = login("dora")
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    dora_char = _char("Dorin")
    c = client.post(f"{API}/campaigns/join", headers=dora, json={"code": code, "character": dora_char}).json()
    dora_member = next(m["id"] for m in c["members"] if m["displayName"] == "Dora")
    s1 = _kapitel(dbs, w, 1, gaeste=("Gastine",))
    _kapitel(dbs, w, 2, veroeffentlicht=False)
    client.post(f"{API}/sessions/{s1}/comments", headers=w["pl"], json={"text": "BEN-OEFFENTLICH"})
    client.post(f"{API}/sessions/{s1}/comments", headers=w["pl"],
                json={"text": "BEN-AN-ANNA", "recipientMemberId": w["gm_member"]})
    client.post(f"{API}/sessions/{s1}/comments", headers=dora, json={"text": "DORA-OEFFENTLICH"})
    geheim = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "npc", "name": "Der Graue Fürst", "summary": "Strippenzieher.", "visibility": "gm_only",
        "gmNotes": "GM-NOTES-FUERST"}).json()
    verborgen = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Die Gruft", "summary": "Unter der Kapelle.", "visibility": "public",
        "hiddenFromMemberIds": [dora_member]}).json()
    bogen = {"file": ("bogen.txt", "Stärke 14".encode(), "text/plain")}
    client.post(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"], files=bogen,
                data={"kind": "character_sheet"})
    client.post(f"{API}/campaigns/{w['cid']}/documents", headers=dora,
                files={"file": ("dora.txt", "DORA-BOGEN".encode(), "text/plain")}, data={"kind": "character_sheet"})
    client.post(f"{API}/campaigns/{w['cid']}/documents", headers=w["gm"],
                files={"file": ("handout.txt", "Ein Brief aus Rabenfels.".encode(), "text/plain")},
                data={"kind": "handout"})
    client.put(f"{API}/campaigns/{w['cid']}/members/me/move-consent", headers=w["pl"], json={"granted": True})
    return {"dora": dora, "dora_member": dora_member, "ben_char": ben_char, "dora_char": dora_char,
            "geheim": geheim, "verborgen": verborgen}


def _exportieren(client, w) -> tuple[dict, bytes]:
    r = client.post(f"{API}/campaigns/{w['cid']}/exports", headers=w["gm"])
    assert r.status_code == 202, r.text
    x = _warten_export(client, w, r.json()["id"])
    roh = TestClient.request(client, "GET", f"{API}{x['downloadUrl']}")  # ohne Token, wie ein Browser
    assert roh.status_code == 200 and roh.headers["content-type"] == "application/zip"
    assert "taleward-kampagne-rabenfels-" in roh.headers["content-disposition"]
    return x, roh.content


def test_zustimmung_und_kennung_nur_fuer_sich_und_sl(client, world, dbs):
    from app.models import ConsentLog

    w = world
    ch = _char("Tharvok")
    client.put(f"{API}/campaigns/{w['cid']}/members/me/character", headers=w["pl"], json=ch)
    url = f"{API}/campaigns/{w['cid']}/members/me/move-consent"
    m = client.put(url, headers=w["pl"], json={"granted": True}).json()
    assert m["moveConsentAt"] is not None and m["openSeat"] is False and m["characterId"] == ch["id"]
    # die SL sieht Zustimmung und Kennung
    sl = {x["id"]: x for x in client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["members"]}
    assert sl[w["pl_member"]]["moveConsentAt"] and sl[w["pl_member"]]["characterId"] == ch["id"]
    # ein anderer Spieler sieht die Zustimmung, aber nicht die Kennung
    w2 = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    client.post(f"{API}/campaigns/join", headers=w["out"], json={"code": w2})
    andere = {x["id"]: x for x in client.get(f"{API}/campaigns/{w['cid']}", headers=w["out"]).json()["members"]}
    assert andere[w["pl_member"]]["moveConsentAt"] is not None
    assert "characterId" not in andere[w["pl_member"]] and "characterVersion" not in andere[w["pl_member"]]
    # Widerruf, beides protokolliert
    assert client.put(url, headers=w["pl"], json={"granted": False}).json()["moveConsentAt"] is None
    dbs.expire_all()
    aktionen = [z.action for z in dbs.query(ConsentLog).filter_by(member_id=w["pl_member"]).order_by(ConsentLog.at)]
    assert aktionen == ["move_granted", "move_revoked"]
    assert client.put(url, headers=w["out"], json={"granted": True}).status_code == 200
    assert client.put(f"{API}/campaigns/{uuid.uuid4()}/members/me/move-consent", headers=w["pl"],
                      json={"granted": True}).status_code == 404


def test_export_inhalt_und_zugang(client, world, dbs, make_user, login):
    w = world
    a = _aufbauen(client, w, dbs, make_user, login)
    # Spieler: Exporte gibt es nicht
    assert client.post(f"{API}/campaigns/{w['cid']}/exports", headers=w["pl"]).status_code == 404
    x, daten = _exportieren(client, w)
    assert x["consentedMemberIds"] == [w["pl_member"]] and x["sizeBytes"] == len(daten) and x["expiresAt"]
    assert client.get(f"{API}/campaigns/{w['cid']}/exports/{x['id']}", headers=w["pl"]).status_code == 404
    z = zipfile.ZipFile(io.BytesIO(daten))
    namen = set(z.namelist())
    alles = b"".join(z.read(n) for n in namen)
    k = json.loads(z.read("kampagne.json"))
    assert k["format"] == "taleward-kampagne/1" and k["apiVersion"] == "0.4.12"
    # nie in der Datei
    for verboten in (GEHEIM, PRUEF, "Anna", "Ben", "Dora", "anna", "HINTERGRUND-DORIN", "DORA-BOGEN",
                     "DORA-OEFFENTLICH", "BEN-AN-ANNA", "Dorin, Kurzbeschreibung."):
        assert verboten.encode() not in alles, verboten
    # mit Zustimmung bzw. SL-Wissen
    for erlaubt in ("HINTERGRUND-THARVOK", "BEN-OEFFENTLICH", "SL-NOTIZ-1", "GM-NOTES-FUERST", "Stärke 14",
                    "Ein Brief aus Rabenfels.", "Gastine"):
        assert erlaubt.encode() in alles, erlaubt
    assert "SL-NOTIZ-2" not in json.dumps(k)  # unveröffentlichtes Kapitel geht nicht mit
    assert [s["number"] for s in k["sessions"]] == [1]
    plaetze = {p["characterName"]: p for p in k["seats"]}
    assert plaetze["Tharvok"]["consented"] and plaetze["Tharvok"]["portrait"] in namen
    assert plaetze["Dorin"]["characterId"] == a["dora_char"]["id"] and plaetze["Dorin"]["character"] is None
    assert plaetze["Dorin"]["portrait"] is None
    # Download: falscher Schlüssel, Bearer der SL, Range
    assert TestClient.request(client, "GET", f"{API}/campaigns/{w['cid']}/exports/{x['id']}/file?t=falsch"
                              ).status_code == 404
    assert client.get(f"{API}/campaigns/{w['cid']}/exports/{x['id']}/file", headers=w["pl"]).status_code == 404
    assert client.get(f"{API}/campaigns/{w['cid']}/exports/{x['id']}/file", headers=w["gm"]).content == daten
    teil = TestClient.request(client, "GET", f"{API}{x['downloadUrl']}", headers={"Range": "bytes=10-19"})
    assert teil.status_code == 206 and teil.content == daten[10:20]
    # Es läuft schon einer → 409
    from app.models import CampaignExport

    dbs.add(CampaignExport(campaign_id=w["cid"], state="queued", consented_member_ids="[]"))
    dbs.commit()
    r = client.post(f"{API}/campaigns/{w['cid']}/exports", headers=w["gm"])
    assert r.status_code == 409 and r.json()["code"] == "export_running"
    # nach 24 h: 410
    from app.db import utcnow

    dbs.expire_all()
    alt = dbs.get(CampaignExport, x["id"])
    alt.expires_at = utcnow() - timedelta(minutes=1)
    dbs.commit()
    r = client.get(f"{API}/campaigns/{w['cid']}/exports/{x['id']}/file", headers=w["gm"])
    assert r.status_code == 410 and r.json()["code"] == "export_expired"
    assert client.get(f"{API}/campaigns/{w['cid']}/exports/{x['id']}", headers=w["gm"]).json()["downloadUrl"] is None
    from app import umzug

    umzug.aufraeumen(dbs)
    assert not umzug.export_pfad(x["id"]).exists()


def _importieren(client, h, daten: bytes, teil: int | None = None) -> dict:
    r = client.post(f"{API}/imports", headers=h, json={"fileName": "umzug.zip", "sizeBytes": len(daten)})
    assert r.status_code == 201, r.text
    start = r.json()
    groesse = start["chunkSizeBytes"]
    assert start["chunkCount"] == -(-len(daten) // groesse)
    iid = start["importId"]
    st = client.get(f"{API}/imports/{iid}", headers=h).json()
    assert st["state"] == "uploading" and st["missingChunks"] == list(range(start["chunkCount"]))
    for i in range(start["chunkCount"]):
        stueck = daten[i * groesse:(i + 1) * groesse]
        r = client.put(f"{API}/imports/{iid}/chunks/{i}", headers={**h, "X-Chunk-SHA256":
                                                                     hashlib.sha256(stueck).hexdigest()},
                       content=stueck)
        assert r.status_code == 204, r.text
    r = client.post(f"{API}/imports/{iid}/complete", headers=h)
    assert r.status_code == 202, r.text
    return client.get(f"{API}/imports/{iid}", headers=h).json()


def test_import_ganzer_weg_und_plaetze(client, world, dbs, make_user, login, monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_BYTES", str(16 * 1024))
    from app import config

    config.get_settings.cache_clear()
    w = world
    a = _aufbauen(client, w, dbs, make_user, login)
    _, daten = _exportieren(client, w)
    make_user("eve")
    eve = login("eve")
    st = _importieren(client, eve, daten)
    assert st["state"] == "done" and st["openSeats"] == 3 and "missingChunks" not in st, st
    cid = st["campaignId"]
    c = client.get(f"{API}/campaigns/{cid}", headers=eve).json()
    assert c["myRole"] == "gm" and c["title"] == "Rabenfels" and c["memberCount"] == 1
    offen = [m for m in c["members"] if m["openSeat"]]
    assert len(offen) == 3 and all(m["userId"] == "" and m["displayName"] == "Offener Platz" for m in offen)
    tharvok = next(m for m in offen if m["characterName"] == "Tharvok")
    assert tharvok["characterBackstory"] == "HINTERGRUND-THARVOK" and tharvok["portraitUpdatedAt"]
    assert tharvok["recordingConsentAt"] is None and tharvok["moveConsentAt"] is None
    dorin = next(m for m in offen if m["characterName"] == "Dorin")
    assert dorin["characterSummary"] is None and dorin["characterId"] == a["dora_char"]["id"]
    kapitel = client.get(f"{API}/campaigns/{cid}/sessions", headers=eve).json()
    assert [(s["number"], s["state"]) for s in kapitel] == [(1, "published")]
    sid = kapitel[0]["id"]
    assert client.get(f"{API}/sessions/{sid}/recap", headers=eve).json()["text"] == "Die Gruppe zog weiter (1)."
    eintraege = {e["name"]: e for e in client.get(f"{API}/campaigns/{cid}/entries", headers=eve).json()}
    assert eintraege["Der Graue Fürst"]["visibility"] == "gm_only"
    assert eintraege["Der Graue Fürst"]["gmNotes"] == "GM-NOTES-FUERST"
    assert eintraege["Die Gruft"]["hiddenFromMemberIds"] == [dorin["id"]]
    docs = client.get(f"{API}/campaigns/{cid}/documents", headers=eve).json()
    assert sorted(d["kind"] for d in docs) == ["character_sheet", "handout"]

    # Ben kommt mit seinem Charakter: setzt sich auf seinen Platz, die SL bekommt einen Hinweis
    code = client.post(f"{API}/campaigns/{cid}/invites", headers=eve).json()["code"]
    c = client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": code, "character": a["ben_char"]}).json()
    ich = next(m for m in c["members"] if m["displayName"] == "Ben")
    assert ich["id"] == tharvok["id"] and ich["role"] == "player" and not ich["openSeat"]
    assert "gmNotices" not in c
    hinweise = client.get(f"{API}/campaigns/{cid}", headers=eve).json()["gmNotices"]
    assert [(n["code"], n["memberId"]) for n in hinweise] == [("seat_claimed", tharvok["id"])]
    # Ben sieht seinen öffentlichen Kommentar wieder, aber nicht Doras Kennung
    assert "characterId" not in next(m for m in c["members"] if m["id"] == dorin["id"])
    # Dora bekommt eine Platz-Einladung (gilt einmal)
    r = client.post(f"{API}/campaigns/{cid}/members/{dorin['id']}/invite", headers=eve)
    assert r.status_code == 201 and r.json()["memberId"] == dorin["id"]
    platz_code = r.json()["code"]
    assert client.post(f"{API}/campaigns/{cid}/members/{tharvok['id']}/invite",
                       headers=eve).json()["code"] == "seat_not_open"
    assert client.post(f"{API}/campaigns/{cid}/members/{dorin['id']}/invite", headers=w["pl"]).status_code == 403
    c = client.post(f"{API}/campaigns/join", headers=a["dora"], json={"code": platz_code}).json()
    assert next(m for m in c["members"] if m["displayName"] == "Dora")["id"] == dorin["id"]
    make_user("fynn")
    r = client.post(f"{API}/campaigns/join", headers=login("fynn"), json={"code": platz_code})
    assert r.status_code == 404  # verbraucht
    # Eve: „Das bin ich“ auf Annas altem Platz
    rest = next(m for m in client.get(f"{API}/campaigns/{cid}", headers=eve).json()["members"] if m["openSeat"])
    assert client.post(f"{API}/campaigns/{cid}/members/{rest['id']}/take", headers=w["pl"]).status_code == 403
    r = client.post(f"{API}/campaigns/{cid}/members/{rest['id']}/take", headers=eve)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["myRole"] == "gm" and len(c["members"]) == 3 and not any(m["openSeat"] for m in c["members"])
    assert next(m for m in c["members"] if m["displayName"] == "Eve")["id"] == rest["id"]
    r = client.post(f"{API}/campaigns/{cid}/members/{tharvok['id']}/take", headers=eve)
    assert r.status_code == 403 and r.json()["code"] == "not_importer"
    # Platz wieder freigeben: Ben verliert den Zugriff; die letzte SL nicht
    r = client.post(f"{API}/campaigns/{cid}/members/{tharvok['id']}/release", headers=eve)
    assert r.status_code == 200 and r.json()["openSeat"] is True and r.json()["userId"] == ""
    assert client.get(f"{API}/campaigns/{cid}", headers=w["pl"]).status_code == 404
    assert client.get(f"{API}/campaigns/{cid}", headers=eve).json()["gmNotices"] == []
    r = client.post(f"{API}/campaigns/{cid}/members/{rest['id']}/release", headers=eve)
    assert r.status_code == 409 and r.json()["code"] == "last_gm"
    r = client.post(f"{API}/campaigns/{cid}/members/{tharvok['id']}/release", headers=eve)
    assert r.json()["code"] == "seat_not_open"
    # und wieder einnehmen
    code = client.post(f"{API}/campaigns/{cid}/invites", headers=eve).json()["code"]
    c = client.post(f"{API}/campaigns/join", headers=w["pl"], json={"code": code, "character": a["ben_char"]}).json()
    assert next(m for m in c["members"] if m["displayName"] == "Ben")["id"] == tharvok["id"]
    config.get_settings.cache_clear()


def _zip(dateien: dict[str, bytes | str]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        for name, inhalt in dateien.items():
            z.writestr(name, inhalt)
    return puffer.getvalue()


def _minimal(**mehr) -> str:
    return json.dumps({"format": "taleward-kampagne/1", "apiVersion": "0.4.8",
                       "campaign": {"title": "Klein"}, **mehr})


@pytest.mark.parametrize("daten,schluessel", [
    (b"kein zip", "import_format"),
    (_zip({"kampagne.json": "{kaputt"}), "import_format"),
    (_zip({"kampagne.json": _minimal(), "../boese.txt": "x"}), "import_unsafe"),
    (_zip({"kampagne.json": _minimal(), "bilder/../../x": "x"}), "import_unsafe"),
    (_zip({"kampagne.json": json.dumps({"format": "taleward-kampagne/2", "campaign": {"title": "x"}})}),
     "import_version"),
    (_zip({"kampagne.json": _minimal(apiVersion="9.0.0")}), "import_version"),
    (_zip({"kampagne.json": _minimal(sessions=[{"key": "s1", "number": 1, "playedAt": "2026-01-01T00:00:00Z",
                                               "attendees": [{"seat": "p9"}]}])}), "import_format"),
])
def test_import_fehler(client, world, daten, schluessel):
    st = _importieren(client, world["gm"], daten)
    assert st["state"] == "failed" and st["message"] == schluessel and st["campaignId"] is None


def test_import_klein_und_grenzen(client, world, monkeypatch, dbs):
    w = world
    st = _importieren(client, w["gm"], _zip({"kampagne.json": _minimal()}))
    assert st["state"] == "done" and st["openSeats"] == 0
    c = client.get(f"{API}/campaigns/{st['campaignId']}", headers=w["gm"]).json()
    assert c["title"] == "Klein" and c["coverPreset"]
    # andere Personen sehen den Import nicht
    assert client.get(f"{API}/imports/{st['id']}", headers=w["pl"]).status_code == 404
    # zu groß
    monkeypatch.setenv("IMPORT_MAX_BYTES", "1000")
    from app import config

    config.get_settings.cache_clear()
    r = client.post(f"{API}/imports", headers=w["gm"], json={"fileName": "x.zip", "sizeBytes": 1001})
    assert r.status_code == 413 and r.json()["code"] == "import_too_large" and r.json()["details"] == {"maxBytes": 1000}
    # fehlende Teile
    r = client.post(f"{API}/imports", headers=w["gm"], json={"fileName": "x.zip", "sizeBytes": 900}).json()
    r = client.post(f"{API}/imports/{r['importId']}/complete", headers=w["gm"])
    assert r.status_code == 409 and r.json()["details"] == {"missingChunks": [0]}
    config.get_settings.cache_clear()
    # liegen gebliebene Importe räumt die Wartung nach 7 Tagen weg
    from app import umzug
    from app.db import utcnow
    from app.models import CampaignImport

    dbs.expire_all()
    for imp in dbs.query(CampaignImport):
        imp.created_at = utcnow() - timedelta(days=8)
    dbs.commit()
    assert umzug.aufraeumen(dbs)["importe_geloescht"] == 2
