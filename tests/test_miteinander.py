"""Paket 2 vor dem Vereinseinsatz: Kommentare, Ungelesen-Zähler, Terminabstimmung."""
from datetime import datetime, timedelta, timezone

import pytest

API = "/api/v1"


@pytest.fixture()
def runde(client, world, dbs):
    """world + zweite Spielerin (cleo) + ein veröffentlichtes und ein unveröffentlichtes Kapitel."""
    from app.models import GameSession

    w = dict(world)
    code = client.post(f"{API}/campaigns/{w['cid']}/invites", headers=w["gm"]).json()["code"]
    joined = client.post(f"{API}/campaigns/join", json={"code": code, "characterName": "Kira"}, headers=w["out"]).json()
    w["pl2"], w["pl2_member"] = w["out"], next(m["id"] for m in joined["members"] if m["displayName"] == "Cleo")

    def kapitel():
        return client.post(f"{API}/campaigns/{w['cid']}/sessions", headers=w["gm"], json={
            "playedAt": "2026-09-20T18:00:00Z",
            "attendees": [{"memberId": w["gm_member"], "consent": True, "consentSource": "on_site"}]}).json()

    w["pub"], w["priv"] = kapitel()["id"], kapitel()["id"]
    s = dbs.get(GameSession, w["pub"])
    s.state, s.published_at = "published", datetime.now(timezone.utc)
    dbs.commit()
    return w


def kommentar(client, h, sid, text, an=None):
    body = {"text": text} | ({"recipientMemberId": an} if an else {})
    return client.post(f"{API}/sessions/{sid}/comments", headers=h, json=body)


def texte(client, h, sid):
    return [k["text"] for k in client.get(f"{API}/sessions/{sid}/comments", headers=h).json()]


# ---------------------------------------------------------------- Kommentare
def test_sichtbarkeit_und_regeln(client, runde):
    w = runde
    pub, priv = w["pub"], w["priv"]
    # Unveröffentlichtes Kapitel: für Spieler gibt es nichts
    assert client.get(f"{API}/sessions/{priv}/comments", headers=w["pl"]).status_code == 404
    assert kommentar(client, w["pl"], priv, "Hallo").status_code == 404
    assert kommentar(client, w["gm"], priv, "Notiz an alle").status_code == 201
    # Veröffentlicht
    assert kommentar(client, w["gm"], pub, "Schön war’s!").status_code == 201
    r = kommentar(client, w["pl"], pub, "Wann geht’s weiter?")
    assert r.status_code == 201 and r.json()["recipientMemberId"] is None and r.json()["editedAt"] is None
    assert kommentar(client, w["pl"], pub, "Psst, SL", an=w["gm_member"]).status_code == 201
    r = kommentar(client, w["pl"], pub, "Psst, Kira", an=w["pl2_member"])
    assert r.status_code == 403 and "Spielleitung" in r.json()["message"]
    assert kommentar(client, w["gm"], pub, "Nur für Mira", an=w["pl_member"]).status_code == 201
    assert kommentar(client, w["pl"], pub, "an mich", an=w["pl_member"]).json()["code"] == "recipient_unknown"
    assert kommentar(client, w["pl"], pub, "x", an="gibtsnicht").json()["code"] == "recipient_unknown"
    assert kommentar(client, w["pl"], pub, "   ").json()["code"] == "comment_empty"
    r = kommentar(client, w["pl"], pub, "x" * 4001)
    assert r.status_code == 413 and r.json()["code"] == "comment_too_long"
    assert kommentar(client, w["pl"], pub, "x" * 4000).status_code == 201
    # Wer sieht was
    assert texte(client, w["pl"], pub)[:4] == ["Schön war’s!", "Wann geht’s weiter?", "Psst, SL", "Nur für Mira"]
    assert texte(client, w["pl2"], pub)[:2] == ["Schön war’s!", "Wann geht’s weiter?"]
    assert "Psst, SL" not in texte(client, w["pl2"], pub) and "Nur für Mira" not in texte(client, w["pl2"], pub)
    assert "Psst, SL" in texte(client, w["gm"], pub)


def test_bearbeiten_und_loeschen(client, runde):
    w = runde
    pub = w["pub"]
    spieler = kommentar(client, w["pl"], pub, "Tippfehlr").json()
    sl = kommentar(client, w["gm"], pub, "Von der SL").json()
    geheim = kommentar(client, w["pl"], pub, "Nur an Anna", an=w["gm_member"]).json()
    assert client.patch(f"{API}/comments/{sl['id']}", headers=w["pl"], json={"text": "x"}).status_code == 403
    r = client.patch(f"{API}/comments/{spieler['id']}", headers=w["pl"], json={"text": "Tippfehler"})
    assert r.status_code == 200 and r.json()["text"] == "Tippfehler" and r.json()["editedAt"]
    assert client.patch(f"{API}/comments/{spieler['id']}", headers=w["pl"], json={"text": ""}).status_code == 400
    # Private Nachrichten anderer gibt es für Dritte nicht
    assert client.patch(f"{API}/comments/{geheim['id']}", headers=w["pl2"], json={"text": "x"}).status_code == 404
    assert client.delete(f"{API}/comments/{geheim['id']}", headers=w["pl2"]).status_code == 404
    assert client.delete(f"{API}/comments/{sl['id']}", headers=w["pl"]).status_code == 403
    assert client.delete(f"{API}/comments/{spieler['id']}", headers=w["gm"]).status_code == 204  # SL moderiert
    # Zweite SL sieht die private Nachricht an Anna nicht
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl2_member']}", headers=w["gm"], json={"role": "gm"})
    assert "Nur an Anna" not in texte(client, w["pl2"], pub)
    assert client.delete(f"{API}/comments/{geheim['id']}", headers=w["pl2"]).status_code == 404
    assert client.delete(f"{API}/comments/{geheim['id']}", headers=w["pl"]).status_code == 204
    assert client.delete(f"{API}/comments/gibtsnicht", headers=w["gm"]).status_code == 404


def test_kommentare_nie_im_sprachmodell(client, runde, dbs):
    from app.models import GameSession
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe

    w = runde
    kommentar(client, w["pl"], w["pub"], "MARKER-KOMMENTAR")
    s = dbs.get(GameSession, w["pub"])
    basis = eingabe_bauen(dbs, s)
    assert "MARKER-KOMMENTAR" not in str(recap_eingabe(basis)) + str(vorschlag_eingabe(dbs, s, basis))


# ---------------------------------------------------------------- Ungelesen
def ungelesen(client, h, w):
    c = next(c for c in client.get(f"{API}/campaigns", headers=h).json() if c["id"] == w["cid"])
    liste = {s["id"]: s["unreadComments"] for s in client.get(f"{API}/campaigns/{w['cid']}/sessions", headers=h).json()}
    return c["unread"]["comments"], liste.get(w["pub"])


def test_ungelesene_kommentare(client, runde):
    w = runde
    pub = w["pub"]
    assert ungelesen(client, w["pl"], w) == (0, 0)
    kommentar(client, w["gm"], pub, "Neu von der SL")
    kommentar(client, w["pl"], pub, "Eigener zählt nicht")
    kommentar(client, w["gm"], pub, "Privat an Mira", an=w["pl_member"])
    assert ungelesen(client, w["pl"], w) == (2, 2)
    assert client.get(f"{API}/sessions/{pub}", headers=w["pl"]).json()["unreadComments"] == 2
    assert ungelesen(client, w["pl2"], w) == (2, 2)  # „Privat an Mira“ sieht Kira nicht, dafür Miras Kommentar
    assert ungelesen(client, w["gm"], w)[0] == 1  # auch die SL: Miras öffentlicher Kommentar
    assert client.post(f"{API}/sessions/{pub}/seen", headers=w["pl"]).status_code == 204
    assert ungelesen(client, w["pl"], w) == (0, 0)
    kommentar(client, w["pl2"], pub, "Noch einer")
    assert ungelesen(client, w["pl"], w) == (1, 1)
    # Unveröffentlichte Kapitel zählen für Spieler nicht
    kommentar(client, w["gm"], w["priv"], "Vorab")
    assert ungelesen(client, w["pl"], w)[0] == 1


# ---------------------------------------------------------------- Terminabstimmung
def zeit(tage: int) -> str:
    t = datetime.now(timezone.utc).replace(microsecond=0, second=0) + timedelta(days=tage)
    return t.isoformat().replace("+00:00", "Z")


def braucht(client, h, w) -> bool:
    return next(c for c in client.get(f"{API}/campaigns", headers=h).json() if c["id"] == w["cid"])["datePollNeedsMyVote"]


def test_terminabstimmung(client, runde):
    w = runde
    url = f"{API}/campaigns/{w['cid']}/date-poll"
    assert client.get(url, headers=w["pl"]).json()["code"] == "no_date_poll"
    assert client.post(url, headers=w["pl"], json={}).status_code == 403
    r = client.post(url, headers=w["gm"], json={"note": "Kapitel 3"})
    assert r.status_code == 201 and r.json()["status"] == "open" and r.json()["note"] == "Kapitel 3"
    poll = r.json()["id"]
    assert client.post(url, headers=w["gm"]).json()["code"] == "date_poll_open"
    assert not braucht(client, w["pl"], w)  # noch keine Termine
    opt = f"{API}/date-polls/{poll}/options"
    r = client.post(opt, headers=w["pl"], json={"startsAt": zeit(7)})
    assert r.status_code == 201
    [o] = r.json()["options"]
    assert o["proposedByMemberId"] == w["pl_member"] and o["votes"] == [{"memberId": w["pl_member"], "answer": "yes"}]
    assert client.post(opt, headers=w["gm"], json={"startsAt": zeit(7)}).json()["code"] == "option_exists"
    assert client.post(opt, headers=w["gm"], json={"startsAt": zeit(-1)}).json()["code"] == "date_in_past"
    o2 = next(x for x in client.post(opt, headers=w["gm"], json={"startsAt": zeit(3)}).json()["options"]
              if x["proposedByMemberId"] == w["gm_member"])
    assert braucht(client, w["pl"], w) and braucht(client, w["pl2"], w) and braucht(client, w["gm"], w)
    stimme = f"{API}/date-polls/{poll}/options"
    assert client.put(f"{stimme}/{o2['id']}/vote", headers=w["pl"], json={"answer": "vielleicht"}).status_code == 400
    r = client.put(f"{stimme}/{o2['id']}/vote", headers=w["pl"], json={"answer": "maybe"})
    assert r.status_code == 200 and r.json()["options"][0]["id"] == o2["id"]  # aufsteigend nach Termin
    assert not braucht(client, w["pl"], w)
    client.put(f"{stimme}/{o2['id']}/vote", headers=w["pl"], json={"answer": "no"})
    votes = next(x for x in client.get(url, headers=w["gm"]).json()["options"] if x["id"] == o2["id"])["votes"]
    assert {v["memberId"]: v["answer"] for v in votes} == {w["gm_member"]: "yes", w["pl_member"]: "no"}
    # Entfernen: nur wer vorgeschlagen hat, oder die SL
    assert client.delete(f"{stimme}/{o['id']}", headers=w["pl2"]).status_code == 403
    assert client.delete(f"{stimme}/{o['id']}", headers=w["pl"]).status_code == 200
    # Festlegen
    assert client.post(f"{API}/date-polls/{poll}/close", headers=w["pl"], json={"optionId": o2["id"]}).status_code == 403
    assert client.post(f"{API}/date-polls/{poll}/close", headers=w["gm"], json={"optionId": "x"}).status_code == 404
    r = client.post(f"{API}/date-polls/{poll}/close", headers=w["gm"], json={"optionId": o2["id"]})
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["chosenOptionId"] == o2["id"]
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["pl"]).json()
    assert c["nextSessionAt"] == o2["startsAt"] and not c["datePollNeedsMyVote"]
    assert client.put(f"{stimme}/{o2['id']}/vote", headers=w["pl"], json={"answer": "yes"}).json()["code"] \
        == "date_poll_closed"
    # Neue Abstimmung abbrechen → es gilt wieder die beendete
    neu = client.post(url, headers=w["gm"]).json()["id"]
    assert client.get(url, headers=w["pl"]).json()["id"] == neu
    assert client.delete(f"{API}/date-polls/{neu}", headers=w["pl"]).status_code == 403
    assert client.delete(f"{API}/date-polls/{neu}", headers=w["gm"]).status_code == 204
    assert client.delete(f"{API}/date-polls/{neu}", headers=w["gm"]).json()["code"] == "date_poll_closed"
    assert client.get(url, headers=w["pl"]).json()["id"] == poll


def test_fremde_sehen_keine_abstimmung(client, runde, make_user, login):
    w = runde
    poll = client.post(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["gm"]).json()["id"]
    make_user("dora")
    fremd = login("dora")
    assert client.get(f"{API}/campaigns/{w['cid']}/date-poll", headers=fremd).status_code == 404
    assert client.post(f"{API}/date-polls/{poll}/options", headers=fremd, json={"startsAt": zeit(2)}).status_code == 404


# ---------------------------------------------------------------- Konto löschen
def test_geloeschtes_konto_im_miteinander(client, runde, dbs):
    from app.models import DateVote

    w = runde
    kommentar(client, w["pl"], w["pub"], "Bleibt stehen")
    poll = client.post(f"{API}/campaigns/{w['cid']}/date-poll", headers=w["gm"]).json()["id"]
    client.post(f"{API}/date-polls/{poll}/options", headers=w["pl"], json={"startsAt": zeit(5)})
    export = client.get(f"{API}/me/export", headers=w["pl"]).json()
    assert [k["text"] for k in export["comments"]] == ["Bleibt stehen"] and export["dateVotes"][0]["answer"] == "yes"
    assert client.request("DELETE", f"{API}/me", headers=w["pl"], json={"password": "geheim123"}).status_code == 204
    ks = client.get(f"{API}/sessions/{w['pub']}/comments", headers=w["gm"]).json()
    assert ks[0]["text"] == "Bleibt stehen" and ks[0]["authorMemberId"] == w["pl_member"]
    namen = {m["id"]: m["displayName"] for m in client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["members"]}
    assert namen[w["pl_member"]] == "Gelöschtes Konto"
    dbs.expire_all()
    assert dbs.query(DateVote).filter_by(member_id=w["pl_member"]).count() == 0
    assert kommentar(client, w["gm"], w["pub"], "an Gelöschte", an=w["pl_member"]).json()["code"] == "recipient_unknown"
