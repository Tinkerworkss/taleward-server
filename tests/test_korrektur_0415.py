"""Schnittstelle 0.4.15: Kapitel per Hinweis korrigieren – Entwurf, Entscheidung, Grenzen, Sichtbarkeit."""
import json
from types import SimpleNamespace

import pytest

from tests.test_pruefung_046 import _zur_pruefung
from tests.test_step2a import API
from tests.test_step2b import _kleine_teile, zusammenfassen  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


def _recap(client, w, sid, wer="gm"):
    return client.get(f"{API}/sessions/{sid}/recap", headers=w[wer])


def _start(client, w, sid, note="Das Schwert heißt Eisenschwert. Pipo stirbt nicht.", base=None):
    if base is None:
        base = _recap(client, w, sid).json()["text"]
    return client.post(f"{API}/sessions/{sid}/recap/revision", headers=w["gm"], json={"note": note, "baseText": base})


def _entscheiden(client, w, sid, annehmen):
    return client.post(f"{API}/sessions/{sid}/recap/revision/decision", headers=w["gm"], json={"accept": annehmen})


def test_entwurf_annehmen(client, world, dbs, tmp_path):
    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]
    vorher = _recap(client, w, sid).json()
    assert vorher["revision"] is None
    # Spieler kommen nicht heran (unveröffentlicht: 404)
    assert client.post(f"{API}/sessions/{sid}/recap/revision", headers=w["pl"],
                       json={"note": "x", "baseText": vorher["text"]}).status_code == 404
    # Eingabe prüfen
    assert _start(client, w, sid, note="  ").json()["code"] == "invalid_input"
    assert _start(client, w, sid, note="x" * 2001).json()["code"] == "invalid_input"
    r = _start(client, w, sid, base="ein anderer Text")
    assert r.status_code == 409 and r.json()["code"] == "recap_changed"
    assert _entscheiden(client, w, sid, True).json()["code"] == "no_revision"
    # Auftrag
    r = _start(client, w, sid)
    assert r.status_code == 202
    rev = r.json()["revision"]
    assert rev["state"] == "running" and rev["changes"] == [] and rev["notes"] == [] and rev["createdAt"]
    assert "note" not in rev and "jobId" not in rev  # der Hinweistext geht nicht zurück
    assert _start(client, w, sid).json()["code"] == "revision_running"
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": "neu"}).json()["code"] \
        == "revision_running"
    assert client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"title": "Nur der Titel"}).status_code == 200
    assert _entscheiden(client, w, sid, True).json()["code"] == "revision_running"
    # Testmodus: die Zentrale rechnet den Auftrag
    assert zusammenfassen(dbs)
    rev = _recap(client, w, sid).json()["revision"]
    assert rev["state"] == "ready" and rev["message"] is None
    assert [c["index"] for c in rev["changes"]] == [0]
    assert rev["changes"][0]["after"].endswith("(Testmodus: korrigiert)")
    assert [n["text"] for n in rev["notes"]] == ["Das Schwert heißt Eisenschwert.", "Pipo stirbt nicht."]
    assert all(n["applied"] and n["indexes"] == [0] for n in rev["notes"])
    # Übernehmen: nur Absatz 0 ändert sich, im Prüfteil ungeprüft
    r = _entscheiden(client, w, sid, True)
    assert r.status_code == 200
    d = r.json()
    assert d["revision"] is None
    alt, neu = vorher["text"].split("\n\n"), d["text"].split("\n\n")
    assert len(alt) == len(neu) and neu[0] == rev["changes"][0]["after"] and neu[1:] == alt[1:]
    p0 = d["review"]["paragraphs"][0]
    assert p0["verdict"] == "unchecked" and p0["note"] is None and p0["evidence"] == []
    assert _entscheiden(client, w, sid, True).json()["code"] == "no_revision"
    # Nach der Veröffentlichung: Spieler sehen kein revision-Feld
    client.post(f"{API}/sessions/{sid}/publish", headers=w["gm"])
    assert "revision" not in _recap(client, w, sid, "pl").json()
    assert _start(client, w, sid).json()["code"] == "wrong_state"


def test_verwerfen_und_von_hand(client, world, dbs, tmp_path):
    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]
    text = _recap(client, w, sid).json()["text"]
    _start(client, w, sid)
    assert zusammenfassen(dbs)
    r = _entscheiden(client, w, sid, False)
    assert r.status_code == 200 and r.json()["revision"] is None and r.json()["text"] == text
    # ein offener Entwurf fällt weg, wenn die SL den Text selbst ändert …
    _start(client, w, sid)
    assert zusammenfassen(dbs)
    assert _recap(client, w, sid).json()["revision"]["state"] == "ready"
    r = client.put(f"{API}/sessions/{sid}/recap", headers=w["gm"], json={"text": text + "\n\nNeuer Absatz."})
    assert r.json()["revision"] is None
    # … und beim Neu schreiben; ein laufender Auftrag liefert danach nichts mehr ab
    _start(client, w, sid)
    assert client.post(f"{API}/sessions/{sid}/resummarize", headers=w["gm"]).status_code == 202
    from app.models import Job

    dbs.expire_all()
    revise = [j for j in dbs.query(Job).filter(Job.session_id == sid, Job.type == "revise")]
    assert revise[-1].state == "failed" and revise[-1].error_code == "revision_discarded"
    assert zusammenfassen(dbs)
    assert _recap(client, w, sid).json()["revision"] is None


def test_grenze_und_ohne_modell(client, world, dbs, tmp_path, monkeypatch):
    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]
    for _ in range(10):
        assert _start(client, w, sid).status_code == 202
        assert zusammenfassen(dbs)
    r = _start(client, w, sid)
    assert r.status_code == 409 and r.json()["code"] == "revision_limit" and "zehnmal" in r.json()["message"]
    from app.models import Job

    dbs.query(Job).filter(Job.type == "revise").delete()
    dbs.commit()
    # Cloud ohne Freigabe der Kampagne: gleich ein fehlgeschlagener Entwurf mit Hinweis, nichts eingereiht
    from app import einstellungen

    monkeypatch.setattr(einstellungen, "llm_konfig", lambda db: SimpleNamespace(art="api", api_key="k"))
    r = _start(client, w, sid)
    rev = r.json()["revision"]
    assert r.status_code == 202 and rev["state"] == "failed" and "Cloud" in rev["message"]
    r = client.post(f"{API}/sessions/{sid}/recap/revision", headers={**w["gm"], "Accept-Language": "en"},
                    json={"note": "x y", "baseText": _recap(client, w, sid).json()["text"]})
    assert "cloud" in r.json()["revision"]["message"]
    assert _entscheiden(client, w, sid, True).json()["code"] == "no_revision"  # failed lässt sich nur verwerfen
    assert _entscheiden(client, w, sid, False).json()["revision"] is None


def test_ergebnis_wird_geprueft(client, world, dbs, tmp_path):
    """Was ein Worker zurückliefert, prüft der Server selbst; ein Fehlschlag setzt die Runde nie auf failed."""
    from app import korrektur, queue
    from app.models import GameSession, Job, Recap

    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]
    _start(client, w, sid)
    dbs.expire_all()
    r = dbs.get(Recap, sid)
    teile = r.text.split("\n\n")
    job = dbs.get(Job, json.loads(r.revision)["jobId"])
    korrektur.ergebnis_speichern(dbs, job, {
        "changes": [{"index": 99, "after": "x"}, {"index": 0, "after": "Neu\n\nzwei Absätze", "before": teile[0]},
                    {"index": 0, "after": "doppelt"}, {"index": 1, "before": "falsches Vorher", "after": "y"}],
        "notes": [{"text": "H1", "indexes": [0, 99]}, {"text": "H2", "indexes": [1]}, {"text": "", "indexes": []}],
    }, "local", None, "m")
    dbs.commit()
    rev = _recap(client, w, sid).json()["revision"]
    assert rev["changes"] == [{"index": 0, "before": teile[0], "after": "Neu zwei Absätze"}]
    assert rev["notes"] == [{"text": "H1", "applied": True, "indexes": [0]},
                            {"text": "H2", "applied": False, "indexes": []}]
    # ein neuer Auftrag ersetzt den Entwurf; der alte Auftrag liefert danach ins Leere
    _start(client, w, sid)
    dbs.expire_all()
    korrektur.ergebnis_speichern(dbs, job, {"changes": [{"index": 0, "after": "zu spät"}], "notes": []}, "local",
                                 None, "m")
    dbs.commit()
    assert _recap(client, w, sid).json()["revision"]["state"] == "running"
    # endgültiger Fehlschlag: nur der Entwurf scheitert
    dbs.expire_all()
    neu = dbs.get(Job, json.loads(dbs.get(Recap, sid).revision)["jobId"])
    queue.fail_job(dbs, neu, "revision_error", "kaputt", retryable=False)
    dbs.commit()
    rev = _recap(client, w, sid).json()["revision"]
    assert rev["state"] == "failed" and rev["message"]
    assert dbs.get(GameSession, sid).state == "awaiting_review"


def test_worker_bekommt_auftrag_nur_mit_faehigkeit(client, world, dbs, tmp_path, monkeypatch):
    from app import einstellungen
    from app.models import Worker
    from tests.test_step2a import worker_token

    w = world
    sid = _zur_pruefung(client, w, dbs, tmp_path)["id"]
    monkeypatch.setattr(einstellungen, "llm_konfig", lambda db: SimpleNamespace(
        art="lokal", api_key=None, lokal_modell="m", lokal_kontext=8192))
    _start(client, w, sid)
    token = worker_token(dbs)
    dbs.query(Worker).update({"capabilities": "asr,llm"})
    dbs.commit()
    h = {"Authorization": f"Bearer {token}"}
    alt = client.post("/worker/v1/jobs/claim", headers=h, json={"capabilities": ["asr", "llm"], "waitSeconds": 0})
    assert alt.status_code == 204  # ältere Worker kennen den Auftrag nicht
    r = client.post("/worker/v1/jobs/claim", headers=h,
                    json={"capabilities": ["asr", "llm", "llm_revise"], "waitSeconds": 0})
    a = r.json()
    assert a["type"] == "revise" and a["files"] == [] and a["revise"]["note"].startswith("Das Schwert")
    assert a["revise"]["text"] and "transkript" not in a["revise"]["recap"] and a["revise"]["model"] == "m"
    teile = a["revise"]["text"].split("\n\n")
    r = client.post(f"/worker/v1/jobs/{a['jobId']}/revision-result", headers=h, json={
        "changes": [{"index": 1, "before": teile[1], "after": "Pipo lebt."}],
        "notes": [{"text": "Pipo stirbt nicht.", "applied": True, "indexes": [1]}], "model": "ollama/m",
        "tokensIn": 10, "tokensOut": 5, "computeSeconds": 1.5})
    assert r.status_code == 204
    rev = _recap(client, w, sid).json()["revision"]
    assert rev["state"] == "ready" and rev["changes"][0]["after"] == "Pipo lebt."


def test_ausfuehren_mit_sprachmodell():
    """Der echte Weg (Zentrale mit Cloud-API, Worker mit lokalem Modell): nur betroffene Absätze, Liste je Hinweis."""
    from app import korrektur
    from app import sprachmodell as sm

    class Klient:
        modell = "m"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            return sm.Antwort(json.dumps({"absaetze": [
                {"nr": 2, "text": "Pipo blieb verletzt\n\nam Ufer zurück.", "hinweise": [2]},
                {"nr": 3, "text": "Ganz neu ohne Hinweis.", "hinweise": []}]}), 10, 5)

    k = Klient()
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [], "bibel": []}
    z = {"recap": ein, "text": "Ana zog das Schwert.\n\nPipo starb am Ufer.\n\nDer Regen hörte auf.",
         "note": "Das Schwert heißt Eisenschwert. Pipo stirbt nicht.", "auszuege": "[1:00] Ana: Pipo lebt!"}
    erg = korrektur.ausfuehren(sm.Ablauf(k), z)
    system, nutzer = k.aufrufe[0]
    assert "Ausschnitte aus der Abschrift (nur zur Orientierung" in nutzer and "H2 (ohne Absatzangabe)" in nutzer
    # Absatz 3 hat das Modell ohne Hinweis geändert – das fällt weg; Hinweis 1 (Schwert) ist nirgends umgesetzt
    assert [c["index"] for c in erg["changes"]] == [1]
    assert erg["changes"][0]["after"] == "Pipo blieb verletzt am Ufer zurück."
    assert erg["notes"] == [{"text": "Das Schwert heißt Eisenschwert.", "applied": False, "indexes": []},
                            {"text": "Pipo stirbt nicht.", "applied": True, "indexes": [1]}]
