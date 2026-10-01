"""Schnittstelle 0.4.6: Prüfteil am Recap, unsichere Namen, Korrektur und erneute Transkription, Namenshilfe."""
import pytest

from tests.test_step2a import API, status, worker_starten, worker_token
from tests.test_step2b import _kleine_teile, transkribiert, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _audio_behalten(dbs):
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "audio.aufbewahrung", "until_release")
    meta_schreiben(dbs, "audio.max_tage", "7")
    dbs.commit()


def _zur_pruefung(client, w, dbs, tmp_path):
    s = transkribiert(client, w, dbs, tmp_path)
    sp = client.get(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"]).json()
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"],
               json=[{"speakerId": sp[0]["id"], "memberId": w["gm_member"]},
                     {"speakerId": sp[1]["id"], "memberId": w["pl_member"]}])
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"
    return s


def test_pruefteil_nur_fuer_die_sl(client, world, dbs, tmp_path):
    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    r = client.get(f"{API}/sessions/{s['id']}/recap", headers=w["gm"]).json()
    rv = r["review"]
    assert rv["state"] == "done" and rv["model"] == "attrappe" and rv["report"]["total"] == 2
    assert [p["verdict"] for p in rv["paragraphs"]] == ["supported", "partial"]
    beleg = rv["paragraphs"][0]["evidence"][0]
    assert beleg["start"] == 0.0 and beleg["speakerMemberId"] == w["gm_member"] and beleg["end"] > 0
    # Bearbeitet die SL den Text, gilt die Prüfung als „vor deiner Änderung“
    r = client.put(f"{API}/sessions/{s['id']}/recap", headers=w["gm"], json={"text": "Neu.\n\nGanz neu."}).json()
    assert r["review"]["stale"] is True and r["review"]["state"] == "done"
    # Spieler: nach der Veröffentlichung Recap ja, Prüfteil nie
    client.post(f"{API}/sessions/{s['id']}/publish", headers=w["gm"])
    r = client.get(f"{API}/sessions/{s['id']}/recap", headers=w["pl"])
    assert r.status_code == 200 and "review" not in r.json()
    assert "review" in client.get(f"{API}/sessions/{s['id']}/recap", headers=w["gm"]).json()


def test_gegenpruefung_abschaltbar(client, world, dbs, tmp_path):
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "pruefung.gegenpruefen", "aus")
    dbs.commit()
    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    # Worker-Auftrag trägt den Schalter
    from app.models import Job
    from app.routers.worker import _zusammenfassungs_auftrag

    job = dbs.query(Job).filter_by(session_id=s["id"], type="summarize").one()
    assert _zusammenfassungs_auftrag(dbs, job)["summarize"]["review"] is False


def test_unsichere_namen_und_korrektur(client, world, dbs, tmp_path):
    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    sid = s["id"]
    # Spieler: nicht einmal die Existenz
    assert client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["pl"]).status_code == 404
    antwort = client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()
    assert antwort["retranscribesLeft"] == 2
    t = antwort["terms"][0]
    assert t["heard"] == "Tharvok" and t["occurrences"] == 2 and t["confidence"] == 0.31 and len(t["examples"]) == 2
    # Spieler: unveröffentlichtes Kapitel gibt es für sie nicht (404)
    assert client.post(f"{API}/sessions/{sid}/corrections", headers=w["pl"],
                       json={"corrections": [{"heard": "Tharvok", "correct": "Darvok"}]}).status_code == 404
    assert client.post(f"{API}/sessions/{sid}/corrections", headers=w["gm"], json={"corrections": []}).status_code == 400
    r = client.post(f"{API}/sessions/{sid}/corrections", headers=w["gm"],
                    json={"corrections": [{"heard": "tharvok", "correct": "Darvok"}]})
    assert r.status_code == 200 and "review" in r.json()
    zeilen = client.get(f"{API}/sessions/{sid}/transcript", headers=w["gm"]).json()
    assert any("Darvok am Tor" in z["text"] for z in zeilen) and not any("Tharvok" in z["text"] for z in zeilen)
    assert client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["terms"] == []
    # Korrektur landet in der Namenshilfe (nur SL sieht sie)
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert c["hotwords"][0] == "Darvok" and "Mira" in c["hotwords"]
    assert "hotwords" not in client.get(f"{API}/campaigns/{w['cid']}", headers=w["pl"]).json()


def test_begriff_ignorieren(client, world, dbs, tmp_path):
    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    client.post(f"{API}/sessions/{s['id']}/corrections", headers=w["gm"],
                json={"corrections": [{"heard": "Tharvok", "correct": ""}]})
    assert client.get(f"{API}/sessions/{s['id']}/uncertain-terms", headers=w["gm"]).json()["terms"] == []
    zeilen = client.get(f"{API}/sessions/{s['id']}/transcript", headers=w["gm"]).json()
    assert any("Tharvok" in z["text"] for z in zeilen)  # Text bleibt


def test_erneute_transkription(client, world, dbs, tmp_path):
    w = world
    _audio_behalten(dbs)
    s = _zur_pruefung(client, w, dbs, tmp_path)
    sid = s["id"]
    vorher = {z["start"]: z["memberId"] for z in client.get(f"{API}/sessions/{sid}/transcript", headers=w["gm"]).json()}
    assert client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["audioAvailable"] is True
    r = client.post(f"{API}/sessions/{sid}/corrections", headers=w["gm"],
                    json={"corrections": [{"heard": "Tharvok", "correct": "Darvok"}], "retranscribe": True})
    assert r.status_code == 202 and r.json()["state"] == "queued" and r.json()["estimatedSeconds"] is not None
    # Der Worker transkribiert nur den Text neu – mit der korrigierten Namenshilfe; die Stimmen bleiben
    assert worker_starten(client, worker_token(dbs, "zweiter"), tmp_path, "zwei").einen_auftrag()
    assert status(client, w["gm"], sid)["state"] == "summarizing"
    zeilen = client.get(f"{API}/sessions/{sid}/transcript", headers=w["gm"]).json()
    assert {z["start"]: z["memberId"] for z in zeilen} == vorher
    assert any("Darvok am Tor" in z["text"] for z in zeilen)
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], sid)["state"] == "awaiting_review"
    assert client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["retranscribesLeft"] == 1
    # Höchstens zweimal
    from app.models import GameSession

    dbs.expire_all()
    dbs.get(GameSession, sid).nachtranskriptionen = 2
    dbs.commit()
    r = client.post(f"{API}/sessions/{sid}/corrections", headers=w["gm"],
                    json={"corrections": [{"heard": "x", "correct": "y"}], "retranscribe": True})
    assert r.status_code == 409 and r.json()["code"] == "retranscribe_limit"


def test_ohne_audio_keine_erneute_transkription(client, world, dbs, tmp_path):
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "audio.aufbewahrung", "immediate")  # Audio gleich nach der Transkription weg
    dbs.commit()
    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    r = client.post(f"{API}/sessions/{s['id']}/corrections", headers=w["gm"],
                    json={"corrections": [{"heard": "Tharvok", "correct": "Darvok"}], "retranscribe": True})
    assert r.status_code == 409 and r.json()["code"] == "audio_gone"
    # Vor der Prüfung (falscher Zustand) geht nichts
    s2 = transkribiert(client, w, dbs, tmp_path)
    r = client.post(f"{API}/sessions/{s2['id']}/corrections", headers=w["gm"],
                    json={"corrections": [{"heard": "a", "correct": "b"}]})
    assert r.status_code == 409 and r.json()["code"] == "wrong_state"


def test_namenshilfe_pflegen(client, world, dbs):
    w = world
    c = client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()
    assert "Mira" in c["hotwords"]
    r = client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"],
                     json={"hotwords": ["Darvok", " Darvok ", "", "Anna", "Ben"]})
    assert r.status_code == 200 and r.json()["hotwords"] == ["Darvok", "Anna", "Ben"]  # Mira herausgenommen
    # Neue Bibel-Einträge kommen weiter von selbst dazu
    client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"],
                json={"type": "npc", "name": "Rabenmutter", "summary": "x", "visibility": "public"})
    assert "Rabenmutter" in client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["hotwords"]
    from app.models import Campaign
    from app.namenshilfe import fuer_kampagne

    dbs.expire_all()
    namen = fuer_kampagne(dbs, dbs.get(Campaign, w["cid"]))
    assert namen[0] == "Darvok" and "Mira" not in namen
    assert client.patch(f"{API}/campaigns/{w['cid']}", headers=w["gm"],
                        json={"hotwords": ["x" * 41]}).status_code == 400
    assert client.patch(f"{API}/campaigns/{w['cid']}", headers=w["pl"], json={"hotwords": ["x"]}).status_code == 403


def test_zwischenschritte_der_zusammenfassung(client, world, dbs, tmp_path):
    from fastapi.testclient import TestClient

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    from app.einstellungen import meta_schreiben

    meta_schreiben(dbs, "llm.art", "lokal")
    dbs.commit()
    token = worker_token(dbs, "llm-pc")
    wc = TestClient(client.app)
    h = {"Authorization": f"Bearer {token}"}
    from app.models import Worker

    dbs.expire_all()
    for wk in dbs.query(Worker).all():
        wk.capabilities = "asr,llm"
    dbs.commit()
    job = wc.post("/worker/v1/jobs/claim", json={"waitSeconds": 0, "capabilities": ["llm"]}, headers=h).json()
    assert job["summarize"]["review"] is True
    assert wc.post(f"/worker/v1/jobs/{job['jobId']}/progress", json={"progress": 0.6, "step": "review"},
                   headers=h).status_code == 204
    st = status(client, w["gm"], s["id"])
    assert st["message"] == "summarizing.review" and st["progress"] == 0.6 and st["estimatedSeconds"] is None


def test_phonetik_und_absaetze():
    from app.sprachmodell import absaetze, pruefung_lesen
    from app.unsicher import aehnlich, phonetik

    assert phonetik("Tharvok") == phonetik("Darvok") == phonetik("Tarwok")
    assert aehnlich("Tharvok", "Darvok") >= 0.8 and aehnlich("Tharvok", "Rabenfels") < 0.5
    assert absaetze("A\n\n  \nB\n\nC") == ["A", "B", "C"]
    befund = pruefung_lesen({"absaetze": [{"nr": 2, "urteil": "Widerspricht", "stellen": ["1:05"],
                                            "begruendung": "**Falsch**"}, {"nr": 9}]}, 2)
    assert befund[0]["verdict"] == "unchecked"
    assert befund[1] == {"index": 1, "verdict": "contradicted", "note": "Falsch",
                         "evidence": [{"start": 65.0, "quote": ""}]}


# ---------------------------------------------------------------- Wörterbücher (Server 0.4.30)
def _wortliste(woerter, sprache="de"):
    import gzip

    from app import woerterbuch

    woerterbuch.ordner().mkdir(parents=True, exist_ok=True)
    with gzip.open(woerterbuch.pfad(sprache), "wt", encoding="utf-8") as f:
        f.write("\n".join(woerter))


def test_woerterbuch_allgemein_und_zusammengesetzt():
    from app.woerterbuch import Bekannt

    b = Bekannt("de", frozenset({"schwert", "platte", "panzer", "fahndung", "plakat", "heil", "trank", "tor"}))
    for w in ("Schwert", "Plattenpanzer", "Fahndungsplakat", "Heiltrank", "Panzer-Tor"):
        assert b.enthaelt(w), w
    for w in ("Tharvok", "Torx", "Pla"):
        assert not b.enthaelt(w), w
    assert not Bekannt("en", frozenset({"platte", "panzer"})).enthaelt("Plattenpanzer")  # nur im Deutschen
    # Nur als Zusammensetzung bekannt und fast wie ein Name der Kampagne → trotzdem nachfragen
    from app.unsicher import _bekannt

    b = Bekannt("de", frozenset({"rabe", "feld", "schwert"}))
    namen = [("Rabenfels", "e1", None)]
    assert not _bekannt("Rabenfeld", b, namen) and _bekannt("Schwert", b, namen) and _bekannt("Rabenfeld", b, [])


def test_unsichere_namen_mit_woerterbuch_und_kampagnentexten(client, world, dbs, tmp_path):
    from app.models import Campaign, TranscriptSegment

    w = world
    s = _zur_pruefung(client, w, dbs, tmp_path)
    sid = s["id"]
    dbs.expire_all()
    seg = dbs.query(TranscriptSegment).filter_by(session_id=sid).order_by(TranscriptSegment.start).first()
    seg.text += " Das Schwert im Plattenpanzer. Die Tavernenwirtin Grimhild lacht."
    seg.unsicher = ('[{"word": "Schwert", "start": 1, "score": 0.2}, {"word": "Plattenpanzer", "start": 2, '
                    '"score": 0.2}, {"word": "Grimhild", "start": 3, "score": 0.2}, {"word": "Tharvok", "start": 3, '
                    '"score": 0.31}]')
    dbs.commit()
    # Ohne Wortliste: alles Großgeschriebene ist verdächtig
    heard = {t["heard"] for t in client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["terms"]}
    assert {"Schwert", "Plattenpanzer", "Grimhild", "Tharvok"} <= heard
    # Mit Wortliste: allgemeine Wörter und Komposita fallen weg
    _wortliste(["schwert", "platte", "panzer", "das", "die", "im"])
    heard = {t["heard"] for t in client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["terms"]}
    assert heard == {"Grimhild", "Tharvok"}
    # Steht der Name in einer SL-Notiz, ist die Schreibweise bekannt; „Darvok“ aus der Welt-Info wird vorgeschlagen
    client.put(f"{API}/sessions/{sid}/gm-note", headers=w["gm"], json={"text": "Die Wirtin heißt Grimhild."})
    c = dbs.get(Campaign, w["cid"])
    c.world_info = "Im Norden herrscht der Fürst Darvok."
    dbs.commit()
    terms = client.get(f"{API}/sessions/{sid}/uncertain-terms", headers=w["gm"]).json()["terms"]
    assert [t["heard"] for t in terms] == ["Tharvok"] and terms[0]["alternatives"][0] == "Darvok"


def test_sicher_gesagtes_aus_anderen_kapiteln(client, world, dbs, tmp_path):
    from app.models import TranscriptSegment

    w = world
    s1 = _zur_pruefung(client, w, dbs, tmp_path)
    s2 = _zur_pruefung(client, w, dbs, tmp_path)
    dbs.expire_all()
    for seg in dbs.query(TranscriptSegment).filter_by(session_id=s1["id"]):
        seg.unsicher = None  # im ersten Kapitel sauber erkannt …
    dbs.commit()
    # … „Tharvok“ fiel dort dreimal (zwei Abschnitte mit dem Namen, dazu einer mehr)
    seg = dbs.query(TranscriptSegment).filter_by(session_id=s1["id"]).first()
    seg.text += " Tharvok!"
    dbs.commit()
    terms = client.get(f"{API}/sessions/{s2['id']}/uncertain-terms", headers=w["gm"]).json()["terms"]
    assert terms == []


def test_wortliste_laden(client, monkeypatch):  # client: eigener Datenordner
    import gzip
    import hashlib

    import httpx

    from app import woerterbuch

    roh = "ich 900\nschwert 50\nseltenes 3\nTaverne 12\n".encode()
    monkeypatch.setitem(woerterbuch.QUELLEN, "de", ("x/de_full.txt", hashlib.sha256(roh).hexdigest()))
    klient = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=roh)))
    assert woerterbuch.herunterladen("de", klient)
    with gzip.open(woerterbuch.pfad("de"), "rt", encoding="utf-8") as f:
        assert f.read().split("\n") == ["ich", "schwert", "taverne"]
    assert "taverne" in woerterbuch.liste("de")
    monkeypatch.setitem(woerterbuch.QUELLEN, "de", ("x/de_full.txt", "0" * 64))
    try:
        woerterbuch.herunterladen("de", klient)
    except ValueError as e:
        assert "Prüfsumme" in str(e)
    else:
        raise AssertionError("keine Prüfung")


def test_wortliste_neue_fassung_per_update(client, monkeypatch):  # client: eigener Datenordner
    import hashlib

    import httpx

    from app import woerterbuch

    roh = "ich 900\nschwert 50\n".encode()
    monkeypatch.setitem(woerterbuch.QUELLEN, "de", ("x/de_full.txt", hashlib.sha256(roh).hexdigest()))
    abrufe = []
    klient = httpx.Client(transport=httpx.MockTransport(lambda r: abrufe.append(r) or httpx.Response(200, content=roh)))
    monkeypatch.setattr(woerterbuch, "_letzter_versuch", {})
    real = woerterbuch.herunterladen
    monkeypatch.setattr(woerterbuch, "herunterladen", lambda sp: real(sp, klient))
    woerterbuch.automatisch(("de",))
    assert len(abrufe) == 1 and woerterbuch.aktuell("de")
    woerterbuch.automatisch(("de",))
    assert len(abrufe) == 1  # aktuell: nichts zu tun
    # Ein Server-Update bringt eine neue Fassung: die Wartung lädt neu, die alte Liste bleibt bis dahin nutzbar
    neu = "ich 900\nschwert 50\ntaverne 20\n".encode()
    monkeypatch.setitem(woerterbuch.QUELLEN, "de", ("x/de_full.txt", hashlib.sha256(neu).hexdigest()))
    assert woerterbuch.bereit("de") and not woerterbuch.aktuell("de")
    klient = httpx.Client(transport=httpx.MockTransport(lambda r: abrufe.append(r) or httpx.Response(200, content=neu)))
    woerterbuch.automatisch(("de",))
    assert len(abrufe) == 2 and "taverne" in woerterbuch.liste("de") and woerterbuch.aktuell("de")
    # Listen aus 0.4.30 ohne Stand-Vermerk gelten als aktuell, wenn QUELLE.txt die Fassung nennt
    (woerterbuch.ordner() / "de.stand").unlink()
    assert woerterbuch.aktuell("de") and (woerterbuch.ordner() / "de.stand").is_file()
