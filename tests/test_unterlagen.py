"""SL-Unterlagen: hochladen, auslesen, Vorschläge (Testmodus, API, Worker mit Ollama), prüfen, übernehmen."""
import json

import httpx
import pytest

from tests.dateien import docx, pdf
from tests.test_step5 import Modell, einstellen

API = "/api/v1"


def hochladen(client, w, name, daten, kind="gm", h=None, titel=None):
    felder = {"kind": kind} | ({"title": titel} if titel else {})
    return client.post(f"{API}/campaigns/{w['cid']}/documents", headers=h or w["gm"], data=felder,
                       files={"file": (name, daten, "application/octet-stream")})


def verarbeiten(dbs) -> bool:
    from app.zusammenfassung import einen_auftrag

    dbs.expire_all()
    return einen_auftrag(dbs)


def bibel(client, w):
    oeff = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "location", "name": "Rabenfels", "summary": "Kleine Stadt am Finsterwald.", "visibility": "public",
        "gmNotes": "MARKER-GMNOTES-BIBEL"}).json()
    return oeff


# ---------------------------------------------------------------- Hochladen und Auslesen
def test_hochladen_pruefungen(client, world, monkeypatch):
    from app import unterlagen

    w = world
    assert hochladen(client, w, "a.pdf", pdf(["Text"]), h=w["pl"]).status_code == 403
    assert hochladen(client, w, "a.pdf", pdf(["Text"]), h=w["out"]).status_code == 404
    assert client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["pl"]).json() == []  # 0.4.7: nur eigene Charakterbögen
    r = hochladen(client, w, "a.exe", b"MZ....")
    assert r.status_code == 400 and r.json()["code"] == "unsupported_document"
    assert hochladen(client, w, "falsch.pdf", b"kein pdf").json()["code"] == "unsupported_document"
    assert hochladen(client, w, "a.pdf", pdf(["Text"]), kind="geheim").status_code == 400
    monkeypatch.setattr(unterlagen, "MAX_BYTES", 100)
    r = hochladen(client, w, "a.txt", b"x" * 101)
    assert r.status_code == 413 and r.json()["code"] == "document_too_large"


def test_pdf_im_testmodus(client, world, dbs):
    w = world
    bibel(client, w)
    r = hochladen(client, w, "Abenteuer.pdf", pdf(["Der Wirt heisst Borin.", "", "Die Burg Rabenfels."]), "mixed")
    assert r.status_code == 201, r.text
    d = r.json()
    assert (d["title"], d["fileName"], d["kind"], d["pageCount"], d["state"]) == \
        ("Abenteuer", "Abenteuer.pdf", "mixed", 3, "queued")
    assert "1 von 3 Seiten" in d["message"]
    assert verarbeiten(dbs)
    d = client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()
    assert d["state"] == "awaiting_review" and d["proposalCount"] == 2 and d["openProposalCount"] == 2
    assert d["worldInfoSuggestion"]
    vs = client.get(f"{API}/documents/{d['id']}/proposals", headers=w["gm"]).json()
    assert all(v["sessionId"] is None and v["documentId"] == d["id"] for v in vs)
    assert vs[0]["evidence"][0]["page"] == 1
    # Für alle außer der SL gibt es die Unterlage nicht
    assert client.get(f"{API}/documents/{d['id']}", headers=w["pl"]).status_code == 404
    assert client.get(f"{API}/documents/{d['id']}/proposals", headers=w["pl"]).status_code == 404
    assert client.patch(f"{API}/proposals/{vs[0]['id']}", headers=w["pl"], json={"decision": "accepted"}).status_code \
        == 404
    assert [x["id"] for x in client.get(f"{API}/campaigns/{w['cid']}/documents", headers=w["gm"]).json()] == [d["id"]]


def test_ohne_text(client, world, dbs):
    w = world
    d = hochladen(client, w, "scan.pdf", pdf(["", ""])).json()
    assert d["state"] == "failed" and "keinen auslesbaren Text" in d["message"]
    r = client.post(f"{API}/documents/{d['id']}/retry", headers=w["gm"])
    assert r.status_code == 409


def test_wartet_auf_sprachmodell(client, world, dbs):
    w = world
    einstellen(dbs, art="aus")
    d = hochladen(client, w, "a.txt", "Ein Text über den Wirt Borin.".encode()).json()
    assert "wartet auf ein Sprachmodell" in d["message"]
    einstellen(dbs, art="lokal")
    assert "Worker mit Sprachmodell" in client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()["message"]
    assert not verarbeiten(dbs)


# ---------------------------------------------------------------- Regeln zur Trennung SL-/Spielerwissen (API)
class Unterlagenmodell(Modell):
    def __init__(self, vorschlaege, welt=None, **kw):
        super().__init__(**kw)
        self.dok_vorschlaege, self.welt = vorschlaege, welt

    def antwort(self, system, nutzer):
        if "Unterlage für eine Pen-&-Paper-Kampagne" in system:
            return {"proposals": self.dok_vorschlaege, "worldInfo": self.welt}
        if "Welt-Hintergrunds" in system:
            return {"worldInfo": "Zusammengeführt."}
        return super().antwort(system, nutzer)


@pytest.fixture()
def api(monkeypatch):
    from app import zusammenfassung
    from app.sprachmodell import OpenAIKlient

    stand = {"modell": Unterlagenmodell([])}

    def klient(k):
        return OpenAIKlient(k.api_url, k.api_key, k.api_modell,
                            httpx.Client(transport=httpx.MockTransport(lambda r: stand["modell"](r))))

    monkeypatch.setattr(zusammenfassung, "api_klient", klient)
    return stand


def vorschlag(**w):
    return {"entryType": "npc", "action": "create", "title": "Borin", "detail": "Wirt mit rotem Bart.",
            "gmNotes": "Spion der Krone.", "confidence": 0.8,
            "evidence": [{"page": 1, "quote": "Der Wirt heisst Borin."}]} | w


@pytest.mark.parametrize("kind", ["handout", "gm", "mixed"])
def test_arten(client, world, dbs, api, kind):
    w = world
    oeff = bibel(client, w)
    api["modell"] = Unterlagenmodell([
        vorschlag(publicSuggested=True, visibilityReason="Steht im Handout"),
        vorschlag(title="Kurt", publicSuggested=True),  # ohne Begründung
        vorschlag(entryType="location", action="update", targetEntryId=oeff["id"], title="Rabenfels",
                  detail="Hat einen Hafen.", gmNotes=None),
        vorschlag(entryType="location", title="rabenfels", detail="Doppelt angelegt?"),  # → update
        vorschlag(entryType="drache", title="Ungültig"),
    ], welt="Eine raue Küstenwelt.")
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    d = hochladen(client, w, "a.pdf", pdf(["Der Wirt heisst Borin. Er lebt in Rabenfels."]), kind).json()
    assert verarbeiten(dbs)
    d = client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()
    vs = client.get(f"{API}/documents/{d['id']}/proposals", headers=w["gm"]).json()
    aufruf = api["modell"].aufrufe[0]["nutzer"]
    assert "MARKER-GMNOTES-BIBEL" in aufruf and "Der Wirt heisst Borin." in aufruf  # Bibel inkl. gmNotes (YAML)
    borin, kurt, hafen = vs  # „rabenfels“ anlegen wurde zum update von Rabenfels und mit dem anderen vereint
    assert hafen["action"] == "update" and hafen["targetEntryId"] == oeff["id"]
    assert "Doppelt angelegt?" in (hafen["detail"] + (hafen.get("gmNotes") or ""))
    if kind == "handout":
        assert all(v["suggestedVisibility"] == "public" and v.get("gmNotes") is None for v in vs)
        assert not any(v["publicSuggested"] for v in vs) and hafen["detail"].startswith("Hat einen Hafen.")
        assert d["worldInfoSuggestion"] == "Eine raue Küstenwelt."
    else:
        assert all(v["suggestedVisibility"] == "gm_only" for v in vs)
        assert borin["publicSuggested"] is (kind == "mixed") and not kurt["publicSuggested"]
        # Neues aus SL-Unterlagen zu einem öffentlichen Eintrag nur im geheimen Teil
        assert hafen["detail"] == "" and "Hat einen Hafen." in hafen["gmNotes"]
        assert d["worldInfoSuggestion"] == ("Eine raue Küstenwelt." if kind == "mixed" else None)


def test_markierte_abschnitte_bleiben_geheim(client, world, dbs, api):
    w = world
    api["modell"] = Unterlagenmodell([vorschlag(detail="Borin ist ein Spion der Krone und plant den Verrat.",
                                                gmNotes=None, publicSuggested=True, visibilityReason="Handout?")])
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    daten = docx(["Borin ist der Wirt der Taverne.", "[SL] Borin ist ein Spion der Krone und plant den Verrat."])
    d = hochladen(client, w, "notizen.docx", daten, "mixed").json()
    assert d["pageCount"] is None
    assert verarbeiten(dbs)
    [v] = client.get(f"{API}/documents/{d['id']}/proposals", headers=w["gm"]).json()
    assert v["detail"] == "" and "Spion" in v["gmNotes"] and not v["publicSuggested"]


def test_pruefen_uebernehmen_und_loeschen(client, world, dbs, api):
    from app import unterlagen
    from app.models import Entry

    w = world
    oeff = bibel(client, w)
    api["modell"] = Unterlagenmodell([
        vorschlag(),
        vorschlag(title="Mara", detail="Schmiedin."),
        vorschlag(entryType="location", action="update", targetEntryId=oeff["id"], title="Rabenfels",
                  detail="Hat einen Hafen.", gmNotes="Schmuggler im Hafen."),
    ], welt="Küste.")
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    d = hochladen(client, w, "a.pdf", pdf(["Der Wirt heisst Borin."]), "mixed").json()
    assert verarbeiten(dbs)
    borin, mara, hafen = client.get(f"{API}/documents/{d['id']}/proposals", headers=w["gm"]).json()
    client.patch(f"{API}/proposals/{borin['id']}", headers=w["gm"], json={"decision": "accepted", "visibility": "public"})
    client.patch(f"{API}/proposals/{hafen['id']}", headers=w["gm"], json={"decision": "accepted"})
    r = client.post(f"{API}/documents/{d['id']}/apply", headers=w["gm"], json={"applyWorldInfo": True})
    assert r.status_code == 200 and r.json()["state"] == "done" and r.json()["openProposalCount"] == 0
    assert client.post(f"{API}/documents/{d['id']}/apply", headers=w["gm"]).status_code == 409
    assert client.patch(f"{API}/proposals/{mara['id']}", headers=w["gm"], json={"decision": "accepted"}).status_code \
        == 409
    dbs.expire_all()
    e = dbs.query(Entry).filter_by(name="Borin").one()
    assert e.visibility == "public" and e.gm_notes == "Spion der Krone."
    assert dbs.query(Entry).filter_by(name="Mara").count() == 0  # offen → verworfen
    rabenfels = dbs.get(Entry, oeff["id"])
    assert rabenfels.summary == "Kleine Stadt am Finsterwald."  # öffentlicher Text unverändert
    assert "Hat einen Hafen." in rabenfels.gm_notes and "Schmuggler" in rabenfels.gm_notes
    assert client.get(f"{API}/campaigns/{w['cid']}", headers=w["gm"]).json()["worldInfo"] == "Küste."
    # Löschen: Datei und Vorschläge weg, übernommene Einträge bleiben
    assert client.delete(f"{API}/documents/{d['id']}", headers=w["pl"]).status_code == 404
    assert client.delete(f"{API}/documents/{d['id']}", headers=w["gm"]).status_code == 204
    assert client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).status_code == 404
    assert not unterlagen.ordner(d["id"]).exists()
    dbs.expire_all()
    assert dbs.query(Entry).filter_by(name="Borin").count() == 1


def test_fehler_und_neustart(client, world, dbs, api):
    w = world
    api["modell"] = Unterlagenmodell([], status=401)
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    d = hochladen(client, w, "a.md", "# Borin\nDer Wirt.".encode()).json()
    assert verarbeiten(dbs)
    d = client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()
    assert d["state"] == "failed" and "API-Schlüssel" in d["message"]
    api["modell"] = Unterlagenmodell([vorschlag()])
    r = client.post(f"{API}/documents/{d['id']}/retry", headers=w["gm"])
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert client.post(f"{API}/documents/{d['id']}/retry", headers=w["gm"]).status_code == 409
    assert verarbeiten(dbs)
    assert client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()["state"] == "awaiting_review"


# ---------------------------------------------------------------- Lange Unterlagen
def test_stuecke_werden_zusammengefuehrt():
    from app.sprachmodell import Antwort, DokumentAblauf

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = 0

        def chat(self, system, nutzer):
            self.aufrufe += 1
            if "Welt-Hintergrunds" in system:
                return Antwort(json.dumps({"worldInfo": "Eine Welt."}))
            teil = int(nutzer.split("Teil ")[1].split(" ")[0])
            return Antwort(json.dumps({"proposals": [
                {"entryType": "npc", "action": "create", "title": "Borin", "detail": f"Fakt {teil}.",
                 "evidence": [{"page": teil, "quote": "x"}], "confidence": 0.5 + teil / 100}],
                "worldInfo": f"Stück {teil}."}), 10, 2)

    ein = {"sprache": "de", "kampagne": "K", "art": "mixed", "titel": "T", "bibel": [],
           "abschnitte": [{"seite": i + 1, "text": "Wort " * 800} for i in range(6)]}
    k = Klient()
    d = DokumentAblauf(k, stueck_tokens=1500).ausfuehren(ein)
    [v] = d["proposals"]
    assert v["detail"].startswith("Fakt 1.") and "Fakt 2." in v["detail"] and len(v["evidence"]) == 3
    assert d["worldInfoSuggestion"] == "Eine Welt." and k.aufrufe > 3


# ---------------------------------------------------------------- Worker mit Ollama
def test_unterlage_im_worker(client, world, dbs, tmp_path):
    from app.models import UsageLog
    from tests.test_step5 import ollama_knecht

    w = world
    modell = Unterlagenmodell([vorschlag()], ollama=True)
    knecht, token = ollama_knecht(client, dbs, tmp_path, modell)
    einstellen(dbs, art="lokal", lokal_modell="ministral-3:8b", lokal_kontext="8192")
    d = hochladen(client, w, "a.pdf", pdf(["Der Wirt heisst Borin."]), "gm").json()
    assert not verarbeiten(dbs)  # die Zentrale fasst dann nie selbst an
    assert knecht.einen_auftrag()
    d = client.get(f"{API}/documents/{d['id']}", headers=w["gm"]).json()
    assert d["state"] == "awaiting_review" and d["proposalCount"] == 1
    [v] = client.get(f"{API}/documents/{d['id']}/proposals", headers=w["gm"]).json()
    assert v["suggestedVisibility"] == "gm_only"
    log = dbs.query(UsageLog).filter_by(document_id=d["id"]).one()
    assert log.engine == "local" and log.model.startswith("ollama/ministral-3:8b")


def test_unterlage_probe(client, world, dbs, api, tmp_path):
    from typer.testing import CliRunner

    from app.cli import app

    api["modell"] = Unterlagenmodell([vorschlag()], welt="Küste.")
    datei = tmp_path / "abenteuer.pdf"
    datei.write_bytes(pdf(["Der Wirt heisst Borin.", ""]))
    assert "Kein API-Schlüssel" in CliRunner().invoke(app, ["unterlage-probe", str(datei)]).output
    einstellen(dbs, art="aus", api_key="sk-test-schluessel-1234")
    r = CliRunner().invoke(app, ["unterlage-probe", str(datei), "--art", "mixed", "--kampagne", world["cid"]])
    assert r.exit_code == 0, r.output
    assert "davon 1 ohne Text" in r.output and "Borin (Seite 1)" in r.output and "Küste." in r.output
