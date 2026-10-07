"""Paket 1 vor dem Vereinseinsatz: Registrierung, Datenexport, Konto löschen, Titel- und Charakterbilder."""
import io
import json

import pytest
from sqlalchemy import select

from tests.test_verwaltung import admin  # noqa: F401 (Fixture)

API = "/api/v1"


def einladung(client, w) -> str:
    return client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]


def daten(**w):
    basis = {"username": "Neu.Ling", "displayName": "Neuling", "password": "Drachenfeuer7",
             "acceptPrivacy": True, "ageConfirmed": True}
    return {**basis, **w}


# ---------------------------------------------------------------- Registrierung
def test_registrieren_und_beitreten(client, world, dbs):
    from app.models import User

    w = world
    code = einladung(client, w)
    url = f"{API}/auth/register"
    assert client.post(url, json=daten(inviteCode="FALSCH-0000")).json()["code"] == "invite_invalid"
    r = client.post(url, json=daten(inviteCode=code, ageConfirmed=False))
    assert r.status_code == 400 and r.json()["code"] == "consent_required"
    assert client.post(url, json=daten(inviteCode=code, password="kurz")).json()["code"] == "weak_password"
    assert client.post(url, json=daten(inviteCode=code, password="neu.ling")).json()["code"] == "weak_password"
    assert client.post(url, json=daten(inviteCode=code, password="aaaaaaaaaa")).json()["code"] == "weak_password"
    assert client.post(url, json=daten(inviteCode=code, username="a b")).json()["code"] == "username_invalid"
    r = client.post(url, json=daten(inviteCode=code.lower()))
    assert r.status_code == 201, r.text
    antwort = r.json()
    assert antwort["user"]["username"] == "neu.ling"
    h = {"Authorization": f"Bearer {antwort['accessToken']}"}
    assert client.get(f"{API}/me", headers=h).json()["displayName"] == "Neuling"
    u = dbs.query(User).filter_by(username="neu.ling").one()
    assert u.privacy_accepted_at is not None and u.age_confirmed_at is not None
    r = client.post(url, json=daten(inviteCode=code, username="NEU.LING"))
    assert r.status_code == 409 and r.json()["code"] == "username_taken"
    # Beitreten wie gewohnt
    assert client.post(f"{API}/campaigns/join", json={"code": code}, headers=h).status_code == 200
    assert client.post(f"{API}/auth/login", json={"username": "Neu.Ling", "password": "Drachenfeuer7"}).status_code == 200


def test_registrierung_abgeschaltet_und_begrenzt(client, world, dbs):
    from app.einstellungen import meta_schreiben

    code = einladung(client, world)
    meta_schreiben(dbs, "registrierung", "closed")
    dbs.commit()
    r = client.post(f"{API}/auth/register", json=daten(inviteCode=code))
    assert r.status_code == 403 and r.json()["code"] == "registration_closed"
    meta_schreiben(dbs, "registrierung", "invite_only")
    dbs.commit()
    for i in range(9):  # zusammen mit dem Versuch oben: 10 erlaubt
        assert client.post(f"{API}/auth/register", json=daten(inviteCode=f"RATEN-{i:04d}")).status_code == 404
    r = client.post(f"{API}/auth/register", json=daten(inviteCode=code))
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests"


# ---------------------------------------------------------------- Export
def test_export(client, world, dbs):
    w = world
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Mira", "characterBackstory": "Aus dem Nordwald."})
    client.put(f"{API}/campaigns/{w['cid']}/recording-consent", headers=w["pl"], json={"granted": True})
    client.put(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}/portrait", headers={**w["pl"],
               "Content-Type": "image/jpeg"}, content=jpeg(300, 300))
    r = client.get(f"{API}/me/export", headers=w["pl"])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["account"]["username"] == "ben" and d["account"]["signInMethods"][0]["kind"] == "password"
    [m] = d["memberships"]
    assert (m["characterName"], m["characterBackstory"], m["role"]) == ("Mira", "Aus dem Nordwald.", "player")
    assert m["recordingConsentAt"] and m["portrait"]["thumb"].endswith("?size=thumb")
    assert [z["action"] for z in d["consentLog"]] == ["granted"]
    assert d["voiceProfile"] is None and d["comments"] == [] and d["dateVotes"] == []
    assert "anna" not in json.dumps(d).lower()  # nichts über andere Personen


# ---------------------------------------------------------------- Löschen
def test_spieler_loescht_konto(client, world, dbs, tmp_path):
    from app import bilder
    from app.models import ConsentLog, Member, User, VoiceProfile

    w = world
    cid = w["cid"]
    client.patch(f"{API}/campaigns/{cid}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Mira", "characterBackstory": "Geheime Herkunft"})
    client.put(f"{API}/campaigns/{cid}/recording-consent", headers=w["pl"], json={"granted": True})
    client.put(f"{API}/campaigns/{cid}/members/{w['pl_member']}/portrait",
               headers={**w["pl"], "Content-Type": "image/jpeg"}, content=jpeg(300, 300))
    s = client.post(f"{API}/campaigns/{cid}/sessions", headers=w["gm"], json={
        "playedAt": "2026-09-20T18:00:00Z", "attendees": [
            {"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"},
            {"memberId": w["pl_member"], "consent": True, "consentSource": "app"}]}).json()
    dbs.add(VoiceProfile(user_id=dbs.get(Member, w["pl_member"]).user_id, status="ready", base_embedding="[1]",
                         learn_from_sessions=False, learned_session_count=0))
    dbs.commit()
    user_id = dbs.get(Member, w["pl_member"]).user_id
    r = client.request("DELETE", f"{API}/me", headers=w["pl"], json={"password": "falsch"})
    assert r.status_code == 401 and r.json()["code"] == "wrong_password"
    assert client.request("DELETE", f"{API}/me", headers=w["pl"], json={"password": "geheim123"}).status_code == 204
    # Anmeldung und Token sind weg
    assert client.get(f"{API}/me", headers=w["pl"]).status_code == 401
    assert client.post(f"{API}/auth/login", json={"username": "ben", "password": "geheim123"}).status_code == 401
    dbs.expire_all()
    assert dbs.get(User, user_id) is None and dbs.get(VoiceProfile, user_id) is None
    m = dbs.get(Member, w["pl_member"])
    assert m.user_id is None and m.deleted_at and m.character_backstory is None and m.portrait_updated_at is None
    assert m.character_name == "Mira"  # bleibt Teil der Geschichte
    assert not (bilder.portrait_ordner(m.id)).exists()
    assert [z.action for z in dbs.query(ConsentLog).filter_by(member_id=m.id).order_by(ConsentLog.at)] \
        == ["granted", "revoked"]  # Widerruf bleibt als Nachweis
    # Für die anderen: „Gelöschtes Konto“, Kapitel bleibt vollständig
    c = client.get(f"{API}/campaigns/{cid}", headers=w["gm"]).json()
    geloescht = next(x for x in c["members"] if x["id"] == w["pl_member"])
    assert geloescht["displayName"] == "Gelöschtes Konto" and geloescht["userId"] == ""
    assert "characterBackstory" not in geloescht or geloescht["characterBackstory"] is None
    assert c["memberCount"] == 1
    sess = client.get(f"{API}/sessions/{s['id']}", headers=w["gm"]).json()
    assert w["pl_member"] in [a.get("memberId") for a in sess["attendees"]]
    # … kann aber nicht mehr als anwesend eingetragen werden
    r = client.post(f"{API}/campaigns/{cid}/sessions", headers=w["gm"], json={
        "playedAt": "2026-09-27T18:00:00Z", "attendees": [
            {"memberId": w["pl_member"], "consent": True, "consentSource": "on_site"}]})
    assert r.json()["code"] == "attendee_unknown"
    assert client.patch(f"{API}/campaigns/{cid}/members/{w['pl_member']}", headers=w["gm"],
                        json={"role": "gm"}).status_code == 404


def test_letzte_spielleitung(client, world, dbs):
    from app.models import Campaign

    w = world
    r = client.request("DELETE", f"{API}/me", headers=w["gm"], json={"password": "geheim123"})
    assert r.status_code == 409 and r.json()["code"] == "last_gm_campaigns"
    assert "„Rabenfels“" in r.json()["message"]
    # Allein in einer eigenen Kampagne: die wird mitgelöscht
    eigene = client.post(f"{API}/campaigns", headers=w["gm"], json={"title": "Solo-Test"}).json()
    ich = client.get(f"{API}/campaigns/{eigene['id']}", headers=w["gm"]).json()["members"][0]["id"]
    s = client.post(f"{API}/campaigns/{eigene['id']}/sessions", headers=w["gm"], json={
        "playedAt": "2026-09-20T18:00:00Z",
        "attendees": [{"memberId": ich, "consent": True, "consentSource": "on_site"}]}).json()
    client.put(f"{API}/sessions/{s['id']}/gm-note", headers=w["gm"], json={"text": "Notiz"})
    client.put(f"{API}/campaigns/{eigene['id']}/cover-image", headers={**w["gm"], "Content-Type": "image/jpeg"},
               content=jpeg(100, 100))
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["gm"], json={"role": "gm"})
    assert client.request("DELETE", f"{API}/me", headers=w["gm"], json={"password": "geheim123"}).status_code == 204
    dbs.expire_all()
    from app import bilder
    from app.models import GameSession
    assert dbs.get(Campaign, eigene["id"]) is None and dbs.get(GameSession, s["id"]) is None
    assert not bilder.cover_ordner(eigene["id"]).exists()
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["pl"]).json()
    assert c["myRole"] == "gm" and any(m["displayName"] == "Gelöschtes Konto" and m["role"] == "gm"
                                       for m in c["members"])


# ---------------------------------------------------------------- Bilder
def jpeg(breite, hoehe, gps=False, drehung=None) -> bytes:
    from PIL import Image

    b = Image.new("RGB", (breite, hoehe), (120, 30, 30))
    exif = Image.Exif()
    if gps:
        exif[0x8825] = {1: "N", 2: (52.0, 31.0, 0.0)}
        exif[0x010F] = "Kamerahersteller"
    if drehung:
        exif[0x0112] = drehung
    puffer = io.BytesIO()
    b.save(puffer, "JPEG", exif=exif.tobytes())
    return puffer.getvalue()


def png_transparent(breite, hoehe) -> bytes:
    from PIL import Image

    b = Image.new("RGBA", (breite, hoehe), (0, 0, 0, 0))
    puffer = io.BytesIO()
    b.save(puffer, "PNG")
    return puffer.getvalue()


def oeffnen(inhalt: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(inhalt))


def test_titelbild(client, world):
    w = world
    url = f"{API}/campaigns/{w['cid']}/cover-image"
    assert client.get(url, headers=w["pl"]).status_code == 404
    bild = {"Content-Type": "image/jpeg"}
    assert client.put(url, headers={**w["pl"], **bild}, content=jpeg(100, 100)).status_code == 403
    assert client.put(url, headers={**w["gm"], "Content-Type": "image/gif"}, content=b"GIF89a").json()["code"] \
        == "unsupported_image"
    assert client.put(url, headers={**w["gm"], **bild}, content=b"kein bild").json()["code"] == "unsupported_image"
    r = client.put(url, headers={**w["gm"], **bild}, content=b"x" * (5 * 1024 * 1024 + 1))
    assert r.status_code == 413 and r.json()["code"] == "image_too_large"
    r = client.put(url, headers={**w["gm"], **bild}, content=jpeg(2400, 1200, gps=True))
    assert r.status_code == 200 and r.json()["coverPreset"] is None and r.json()["coverImageUpdatedAt"]
    r = client.get(url, headers=w["pl"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    b = oeffnen(r.content)
    assert b.size == (1600, 800) and not b.getexif()  # verkleinert, ohne Metadaten
    assert client.get(url, headers=w["out"]).status_code == 404
    # Mitgeliefertes Motiv ersetzt das eigene Bild
    client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"], json={"coverPreset": "forest"})
    assert client.get(url, headers=w["pl"]).status_code == 404
    client.put(url, headers={**w["gm"], **bild}, content=jpeg(400, 300))
    assert client.delete(url, headers=w["gm"]).status_code == 204
    assert client.get(url, headers=w["gm"]).status_code == 404


def test_charakterbild(client, world):
    w = world
    basis = f"{API}/campaigns/{w['cid']}/members"
    url = f"{basis}/{w['pl_member']}/portrait"
    bild = {"Content-Type": "image/jpeg"}
    assert client.put(url, headers={**w["gm"], **bild}, content=jpeg(300, 300)).status_code == 403  # nur selbst
    r = client.put(url + "?cropX=250&cropY=0&cropSize=100", headers={**w["pl"], **bild}, content=jpeg(300, 200))
    assert r.status_code == 400 and r.json()["code"] == "invalid_crop"
    r = client.put(url + "?cropX=50&cropY=20&cropSize=150", headers={**w["pl"], **bild}, content=jpeg(2000, 1000))
    assert r.status_code == 200 and r.json()["portraitUpdatedAt"]
    full = oeffnen(client.get(url, headers=w["gm"]).content)
    thumb = oeffnen(client.get(url + "?size=thumb", headers=w["gm"]).content)
    assert full.size == (1024, 512) and thumb.size == (256, 256)
    # EXIF-Drehung (6 = 90° im Uhrzeigersinn) wird angewendet, bevor die Metadaten wegfallen
    client.put(url, headers={**w["pl"], **bild}, content=jpeg(400, 200, drehung=6))
    assert oeffnen(client.get(url, headers=w["pl"]).content).size == (200, 400)
    # Transparenz bleibt (WebP)
    client.put(url, headers={**w["pl"], "Content-Type": "image/png"}, content=png_transparent(300, 300))
    r = client.get(url + "?size=thumb", headers=w["pl"])
    assert r.headers["content-type"] == "image/webp" and oeffnen(r.content).mode == "RGBA"
    # Löschen: Spieler nur das eigene, die SL jedes
    gm_url = f"{basis}/{w['gm_member']}/portrait"
    client.put(gm_url, headers={**w["gm"], **bild}, content=jpeg(300, 300))
    assert client.delete(gm_url, headers=w["pl"]).status_code == 403
    assert client.delete(url, headers=w["gm"]).status_code == 204
    assert client.get(url, headers=w["pl"]).status_code == 404
    assert client.get(url, headers=w["out"]).status_code == 404


@pytest.mark.parametrize("kaputt", [b"\xff\xd8\xff\xe0" + b"0" * 100, b""])
def test_kaputte_bilder(client, world, kaputt):
    w = world
    r = client.put(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}/portrait",
                   headers={**w["pl"], "Content-Type": "image/jpeg"}, content=kaputt)
    assert r.status_code == 400 and r.json()["code"] == "unsupported_image"


def test_verwaltung_registrierung(client, dbs, admin):  # noqa: F811
    seite = client.get("/verwaltung/einstellungen").text
    assert 'value="invite_only" selected' in seite
    r = client.post("/verwaltung/einstellungen", data={"csrf": admin, "server_name": "S", "server_operator": "V",
                                                       "min_age": "16", "registrierung": "closed"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"{API}/info").json()["registration"] == "closed"


def test_konto_loeschen_seite_ohne_app(client, world, dbs):
    """Öffentliche Seite /konto-loeschen (Store-Eintrag): Benutzername + Passwort + Haken, Fehler wie DELETE /me."""
    from app.models import User

    w = world
    seite = client.get("/konto-loeschen")
    assert seite.status_code == 200 and "Konto löschen" in seite.text and "Gelöschtes Konto" in seite.text
    assert seite.headers.get("content-security-policy")  # Sicherheits-Kopfzeilen wie auf den anderen Seiten
    url = "/konto-loeschen"
    # ohne Haken, falsches Passwort, Konto einer einzigen SL
    r = client.post(url, data={"username": "ben", "password": "geheim123"})
    assert r.status_code == 400 and "Haken" in r.text
    r = client.post(url, data={"username": "ben", "password": "falsch", "bestaetigt": "1"})
    assert r.status_code == 401 and "stimmen nicht" in r.text
    r = client.post(url, data={"username": "anna", "password": "geheim123", "bestaetigt": "1"})
    assert r.status_code == 409 and "einzige Spielleitung" in r.text
    assert client.post(f"{API}/auth/login", json={"username": "anna", "password": "geheim123"}).status_code == 200
    # Spieler: klappt, danach ist das Konto weg und „Gelöschtes Konto“ bleibt
    r = client.post(url, data={"username": "Ben", "password": "geheim123", "bestaetigt": "1"})
    assert r.status_code == 200 and "Konto gelöscht" in r.text
    dbs.expire_all()
    assert dbs.scalar(select(User).where(User.username == "ben")) is None
    assert client.get(f"{API}/me", headers=w["pl"]).status_code == 401
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert next(x for x in c["members"] if x["id"] == w["pl_member"])["displayName"] == "Gelöschtes Konto"
    # unbekannter Name zählt als Fehlversuch, verrät aber nichts
    r = client.post(url, data={"username": "niemand", "password": "geheim123", "bestaetigt": "1"})
    assert r.status_code == 401 and "stimmen nicht" in r.text
    # Datenschutzseite verweist auf die Seite; Sprachumschalter führt zurück
    r = client.get("/verwaltung/sprache/en", params={"weiter": "/konto-loeschen"}, follow_redirects=False)
    assert r.headers["location"] == "/konto-loeschen"
