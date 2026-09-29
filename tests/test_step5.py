"""Schritt 5: Recap und Vorschläge mit Sprachmodell – über API (Zentrale) oder Ollama (Worker).

Die Anbieter sind nachgebaut (httpx.MockTransport). Geprüft wird vor allem der Spoilerschutz der Aufrufe.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_step2a import API, status, worker_token
from tests.test_step2b import _kleine_teile, bibel, transkribiert, zusammenfassen  # noqa: F401 (Fixture)
from tests.test_verwaltung import admin  # noqa: F401 (Fixture)

pytestmark = pytest.mark.usefixtures("_kleine_teile")


# ---------------------------------------------------------------- nachgebautes Sprachmodell
class Modell:
    """Antwortet je nach Aufgabe (erkannt an der Anweisung) mit passendem JSON."""

    def __init__(self, vorschlaege=None, status=200, ollama=False):
        self.aufrufe, self.status, self.ollama = [], status, ollama
        self.vorschlaege = vorschlaege or []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Szenennotizen" in system:
            return {"notizen": ["[0:00] Die Gruppe reitet nach Rabenfels."]}
        if "Recap" in system:
            return {"title": "Kapitel 1: Der Ritt", "text": "Die Gruppe ritt nach Rabenfels.\n\nDort wartete Regen.",
                    "openThreads": ["Wer hat den Brief geschrieben?", "  "]}
        return {"proposals": self.vorschlaege}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        pfad = request.url.path
        if pfad.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.12.0"})
        if pfad.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "ministral-3:8b", "digest": "abcdef1234567890"}]})
        if pfad.endswith("/api/generate"):
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        system, nutzer = body["messages"][0]["content"], body["messages"][1]["content"]
        self.aufrufe.append({"system": system, "nutzer": nutzer, "body": body})
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        inhalt = json.dumps(self.antwort(system, nutzer), ensure_ascii=False)
        if self.ollama:
            return httpx.Response(200, json={"message": {"content": inhalt}, "prompt_eval_count": 1000,
                                             "eval_count": 100})
        return httpx.Response(200, json={"choices": [{"message": {"content": inhalt}}],
                                         "usage": {"prompt_tokens": 100_000, "completion_tokens": 4_000}})

    def recap_aufruf(self):
        return next(a for a in self.aufrufe if "Recap" in a["system"])

    def vorschlags_aufruf(self):
        return next(a for a in self.aufrufe if "Kampagnen-Bibel" in a["system"])


def einstellen(dbs, **werte):
    from app.einstellungen import meta_schreiben

    for k, v in werte.items():
        meta_schreiben(dbs, f"llm.{k}", v)
    if werte.get("art") == "api":  # 0.3.10: Cloud nur mit Erlaubnis der SL – in diesen Tests erteilt
        from sqlalchemy import update

        from app.models import Campaign

        dbs.execute(update(Campaign).values(allow_cloud_summary=True))
    dbs.commit()


@pytest.fixture()
def api(monkeypatch):
    """API-Betrieb mit nachgebautem Anbieter."""
    from app import zusammenfassung
    from app.sprachmodell import OpenAIKlient

    modell = Modell()

    def klient(k):
        return OpenAIKlient(k.api_url, k.api_key, k.api_modell, httpx.Client(transport=httpx.MockTransport(modell)))

    monkeypatch.setattr(zusammenfassung, "api_klient", klient)
    return modell


def mit_geheimnissen(client, w, dbs, tmp_path):
    """Session bereit zum Zusammenfassen – mit allem, was nie (bzw. nur in den Vorschlags-Aufruf) darf."""
    from app.models import TranscriptSegment

    oeff, geheim = bibel(client, w)
    client.patch(f"{API}/campaigns/{w['cid']}/members/{w['pl_member']}", headers=w["pl"],
                 json={"characterName": "Mira", "characterBackstory": "MARKER-HINTERGRUND"})
    teilweise = client.post(f"{API}/campaigns/{w['cid']}/entries", headers=w["gm"], json={
        "type": "faction", "name": "Die Verschwörer", "summary": "MARKER-NUR-FUER-EINIGE", "visibility": "public",
        "hiddenFromMemberIds": [w["pl_member"]]}).json()
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/gm-note", headers=w["gm"], json={"text": "MARKER-SL-NOTIZ"})
    dbs.add(TranscriptSegment(session_id=s["id"], position=99, start=40.0, end=44.0, speaker_id=None,
                              text="Wir reiten nach Rabenfels, dort treffen wir den Grauen Fürst und die Verschwörer."))
    dbs.commit()
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    return s, oeff, geheim, teilweise


# ---------------------------------------------------------------- API in der Zentrale
def test_api_spoilerschutz_und_ergebnis(client, world, dbs, tmp_path, api):
    from app.models import UsageLog

    w = world
    s, oeff, geheim, teilweise = mit_geheimnissen(client, w, dbs, tmp_path)
    api.vorschlaege = [
        {"entryType": "npc", "action": "create", "title": "Der Wirt", "detail": "Brummig, schenkt Met aus.",
         "suggestedVisibility": "public", "confidence": 0.9, "evidence": [
             {"start": "0:40", "quote": "Wir reiten"},
             {"start": "0:41", "quote": "Der Wirt verrät uns das Versteck der Verschwörer"}]},  # erfunden
        {"entryType": "npc", "action": "reveal", "targetEntryId": geheim["id"], "title": "Der Graue Fürst",
         "detail": "Ein Adliger in Grau.", "gmNotes": "vom Modell – wird ersetzt", "confidence": 0.8},
        {"entryType": "location", "action": "update", "targetEntryId": oeff["id"], "title": "Rabenfels",
         "detail": "Laut Wirt: MARKER-GMNOTES-OEFFENTLICH ist das große Geheimnis der kleinen Stadt dort.",
         "suggestedVisibility": "public", "confidence": 0.7},
        {"entryType": "drache", "action": "create", "title": "Ungültiger Typ", "detail": "x"},
        {"entryType": "npc", "action": "update", "targetEntryId": "gibtsnicht", "title": "Fremd", "detail": "x"},
        {"entryType": "item", "action": "reveal", "targetEntryId": oeff["id"], "title": "Schon öffentlich"},
    ]
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    assert zusammenfassen(dbs)
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"

    recap, vorschlag = api.recap_aufruf(), api.vorschlags_aufruf()
    assert len(api.aufrufe) == 2 and recap["body"]["response_format"] == {"type": "json_object"}
    # Recap: nichts Geheimes, nicht einmal der Name des geheimen Eintrags
    roh = recap["nutzer"] + recap["system"]
    for verboten in ("MARKER", "Der Graue Fürst", "gmNotes"):
        assert verboten not in roh, verboten
    assert "Kleine Stadt am Finsterwald." in roh and "Mira" in roh
    # Vorschläge: ganze Bibel inkl. gmNotes (Schnittstelle) – aber nie SL-Notiz der Session oder Hintergrund
    roh = vorschlag["nutzer"]
    for erlaubt in ("MARKER-GMNOTES-OEFFENTLICH", "MARKER-GEHEIMER-TEXT", "MARKER-GMNOTES-GEHEIM",
                    "MARKER-NUR-FUER-EINIGE", f"id={geheim['id']}"):
        assert erlaubt in roh, erlaubt
    for verboten in ("MARKER-SL-NOTIZ", "MARKER-HINTERGRUND"):
        assert verboten not in roh and verboten not in recap["nutzer"]

    r = client.get(f"{API}/sessions/{s['id']}/recap", headers=w["gm"]).json()
    assert r["title"] == "Kapitel 1: Der Ritt" and r["openThreads"] == ["Wer hat den Brief geschrieben?"]
    vs = {v["title"]: v for v in client.get(f"{API}/sessions/{s['id']}/proposals", headers=w["gm"]).json()}
    assert set(vs) == {"Der Wirt", "Der Graue Fürst", "Rabenfels"}  # Ungültiges fällt weg
    assert vs["Der Wirt"]["evidence"] == [{"start": 40.0, "quote": "Wir reiten"}]  # erfundenes Zitat fällt weg
    assert vs["Der Wirt"]["confidence"] == 0.9 and "low_confidence" not in vs["Der Wirt"]["flags"]
    # Ohne auffindbaren Beleg: markiert, nicht gestrichen – die SL entscheidet
    assert vs["Der Graue Fürst"]["confidence"] <= 0.3 and "low_confidence" in vs["Der Graue Fürst"]["flags"]
    assert vs["Der Graue Fürst"]["gmNotes"] == "MARKER-GEHEIMER-TEXT\n\nMARKER-GMNOTES-GEHEIM"  # vom Server
    assert vs["Der Graue Fürst"]["suggestedVisibility"] == "public"
    # Geheimes im öffentlichen Teil → nur für die SL, mit Hinweis
    assert vs["Rabenfels"]["suggestedVisibility"] == "gm_only"
    assert "geheimen Notizen" in vs["Rabenfels"]["visibilityReason"] and "low_confidence" in vs["Rabenfels"]["flags"]
    log = dbs.query(UsageLog).filter_by(session_id=s["id"], kind="summary").one()
    assert (log.engine, log.model, log.tokens_in, log.tokens_out) == ("external", "mistral-large-latest",
                                                                     200_000, 8_000)
    assert log.cost_cents == round((200_000 * 50 + 8_000 * 150) / 1e6)  # Preistabelle Mistral Large
    # Spieler sehen weiterhin nichts davon
    assert client.get(f"{API}/sessions/{s['id']}/proposals", headers=w["pl"]).status_code == 404


def test_api_fehler(client, world, dbs, tmp_path, api):
    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    einstellen(dbs, art="api", api_key="sk-test-schluessel-1234")
    api.status = 401
    assert zusammenfassen(dbs)
    st = status(client, w["gm"], s["id"])
    assert st["state"] == "failed" and "API-Schlüssel" in st["message"]  # sofort, kein neuer Versuch


def test_api_schluessel_der_externen_transkription(client, world, dbs, tmp_path, api):
    from app.einstellungen import llm_konfig, meta_schreiben

    einstellen(dbs, art="api")
    assert not llm_konfig(dbs).bereit
    meta_schreiben(dbs, "extern.api_key", "sk-mistral-extern-12345")
    dbs.commit()
    k = llm_konfig(dbs)
    assert k.bereit and k.api_key == "sk-mistral-extern-12345" and not k.eigener_key
    einstellen(dbs, api_url="https://anderer.example/v1")
    assert not llm_konfig(dbs).bereit  # fremder Anbieter bekommt nie den Mistral-Schlüssel


def test_status_hinweise(client, world, dbs, tmp_path):
    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    client.put(f"{API}/sessions/{s['id']}/speakers", headers=w["gm"], json=[])
    einstellen(dbs, art="api")
    assert not zusammenfassen(dbs)
    assert "abgeschaltet" in status(client, w["gm"], s["id"])["message"]
    einstellen(dbs, art="lokal")
    assert not zusammenfassen(dbs)  # die Zentrale fasst dann nie selbst zusammen
    assert "kein Sprachmodell verfügbar" in status(client, w["gm"], s["id"])["message"]
    assert "language model" in status(client, w["gm"], s["id"], "en")["message"]


# ---------------------------------------------------------------- lange Sessions: erst Szenennotizen
def test_szenennotizen_sehen_die_bibel_nie():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            return Antwort(json.dumps(Modell().antwort(system, nutzer)), 10, 5)

    zeilen = [{"start": i * 20.0, "sprecher": "Mira" if i % 2 else "Anna (Spielleitung)", "member_id": None,
               "text": f"Satz {i} über Rabenfels und den langen Weg dorthin, es regnet seit Tagen."} for i in range(400)]
    ein = {"sprache": "de", "kampagne": "K", "system": None, "system_name": None, "welt": None,
           "session_nummer": 3, "session_titel": None, "gaeste": [],
           "personen": [{"member_id": "m", "name": "Ben", "rolle": "player", "charakter": "Mira",
                         "charakter_kurz": None}], "transkript": zeilen,
           "bibel": [{"id": "e1", "typ": "location", "name": "Rabenfels", "zusammenfassung": "BIBEL-TEXT"}],
           "geheim": []}
    vorschlag = {**ein, "geheim": [{"id": "g1", "typ": "npc", "name": "Graf", "zusammenfassung": "GEHEIM",
                                    "gm_notes": "GMNOTES"}]}
    k = Klient()
    d = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=1500).ausfuehren(ein, vorschlag)
    notizen = [n for s, n in k.aufrufe if "Szenennotizen" in s]
    assert len(notizen) > 3 and all("BIBEL-TEXT" not in n and "GEHEIM" not in n for n in notizen)
    assert "[0:00] Die Gruppe reitet nach Rabenfels." in k.aufrufe[-1][1]  # Vorschläge arbeiten mit den Notizen
    assert d["tokensIn"] == 10 * len(k.aufrufe) and d["text"].startswith("Die Gruppe ritt")


def test_pruefen_und_zeiten():
    from app.sprachmodell import _erwaehnt, pruefen, transkript_zeilen, zeit_lesen

    assert zeit_lesen("1:02:03") == 3723 and zeit_lesen("12:34") == 754 and zeit_lesen(5) == 5.0
    assert zeit_lesen("abc") is None
    v = pruefen([{"entryType": "npc", "action": "create", "title": "X", "confidence": "0.2",
                  "flags": ["joke_suspected", "unbekannt"], "gmNotes": "g", "targetEntryId": "e1"},
                 {"entryType": "npc", "action": "reveal", "targetEntryId": "g1", "title": "Y",
                  "suggestedVisibility": "gm_only", "gmNotes": "vom Modell"}], {"e1"}, {"g1"})
    assert v[0]["flags"] == ["joke_suspected", "low_confidence"] and v[0]["targetEntryId"] is None
    assert v[0]["gmNotes"] == "g" and v[1]["suggestedVisibility"] == "public" and v[1]["gmNotes"] is None
    assert _erwaehnt("Der Graue Fürst", "wir treffen den grauen fürst") and not _erwaehnt("Rabenfels", "Wald")
    zeilen = transkript_zeilen([{"start": 0, "sprecher": "A", "text": "Hallo"}, {"start": 3, "sprecher": "A",
                                "text": "du"}, {"start": 3700, "sprecher": "B", "text": "Tschüss"}])
    assert zeilen == ["[0:00] A: Hallo du", "[1:01:40] B: Tschüss"]


# ---------------------------------------------------------------- Lokales Modell (Ollama im Worker)
def ollama_knecht(client, dbs, tmp_path, modell):
    from app.worker_prozess import WorkerProzess, lokales_sprachmodell, verarbeite_attrappe

    token = worker_token(dbs)
    from app.models import Worker
    dbs.get(Worker, token.split(".")[1]).capabilities = "asr,llm"
    dbs.commit()
    fn, llm = lokales_sprachmodell("http://ollama", httpx.Client(transport=httpx.MockTransport(modell)))
    assert llm == "Ollama 0.12.0"
    return WorkerProzess("http://testserver", token, tmp_path / "ollama", verarbeite_attrappe,
                        client=TestClient(client.app), claim_wait=0, info={"llm": llm}, zusammenfassen=fn), token


def test_ollama_im_worker_starten(client, world, dbs, tmp_path):
    from app.models import UsageLog

    w = world
    s, oeff, geheim, _ = mit_geheimnissen(client, w, dbs, tmp_path)
    modell = Modell(ollama=True, vorschlaege=[
        {"entryType": "npc", "action": "reveal", "targetEntryId": geheim["id"], "title": "Der Graue Fürst",
         "detail": "Ein Adliger in Grau.", "confidence": 0.8}])
    knecht, token = ollama_knecht(client, dbs, tmp_path, modell)
    # Solange die Verwaltung nicht „Lokales Modell“ eingestellt hat, bekommt er keine Zusammenfassung
    einstellen(dbs, art="aus")
    assert not knecht.einen_auftrag()
    einstellen(dbs, art="lokal", lokal_modell="ministral-3:8b", lokal_kontext="8192")
    assert not zusammenfassen(dbs)
    assert knecht.einen_auftrag()
    assert status(client, w["gm"], s["id"])["state"] == "awaiting_review"
    recap = modell.recap_aufruf()
    assert recap["body"]["options"]["num_ctx"] == 8192 and recap["body"]["format"] == "json"
    assert "MARKER" not in recap["nutzer"] and "Der Graue Fürst" not in recap["nutzer"]
    assert "MARKER-SL-NOTIZ" not in modell.vorschlags_aufruf()["nutzer"]
    vs = client.get(f"{API}/sessions/{s['id']}/proposals", headers=w["gm"]).json()
    assert [v["action"] for v in vs] == ["reveal"] and vs[0]["gmNotes"].startswith("MARKER-GEHEIMER-TEXT")
    log = dbs.query(UsageLog).filter_by(session_id=s["id"], kind="summary").one()
    assert log.engine == "local" and log.worker_id == token.split(".")[1] and log.cost_cents == 0
    assert log.model == "ollama/ministral-3:8b@abcdef123456"


def test_ohne_ollama_nur_transkription():
    from app.worker_prozess import lokales_sprachmodell

    def aus(_req):
        raise httpx.ConnectError("nicht erreichbar")

    assert lokales_sprachmodell("http://ollama", httpx.Client(transport=httpx.MockTransport(aus))) == (None, None)


# ---------------------------------------------------------------- Verwaltung
def test_verwaltung_zusammenfassung(client, dbs, admin):  # noqa: F811
    from app.einstellungen import llm_konfig

    seite = client.get("/verwaltung/zusammenfassung").text
    assert "Wer fasst zusammen?" in seite and 'aria-current="page"' in seite
    url = "/verwaltung/zusammenfassung"
    basis = {"csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-large-latest",
             "lokal_modell": "ministral-3:8b", "lokal_kontext": "12288"}
    r = client.post(url, data=basis)
    assert r.status_code == 400 and "API-Schlüssel eintragen" in r.text
    assert client.post(url, data={**basis, "lokal_kontext": "100"}).status_code == 400
    assert client.post(url, data={**basis, "cent_ein": "50"}).status_code == 400
    assert client.post(url, data={**basis, "anbieter": "andere", "api_url": "http://x.example"}).status_code == 400
    r = client.post(url, data={**basis, "api_key": "sk-abcdefghijklmnop1234"}, follow_redirects=False)
    assert r.status_code == 303
    k = llm_konfig(dbs)
    assert (k.art, k.api_key, k.api_url) == ("api", "sk-abcdefghijklmnop1234", "https://api.mistral.ai/v1")
    seite = client.get("/verwaltung/zusammenfassung").text
    assert "sk-abcdefghijklmnop1234" not in seite and "…1234" in seite and "Kosten etwa 6 Cent" in seite
    client.post(url, data={**basis, "art": "lokal", "lokal_modell": "qwen3:8b"})
    dbs.expire_all()
    assert llm_konfig(dbs).art == "lokal" and llm_konfig(dbs).lokal_modell == "qwen3:8b"
    assert "Noch kein Worker mit Ollama" in client.get("/verwaltung/zusammenfassung").text


def test_recap_probe_speichert_nichts(client, world, dbs, tmp_path, api):
    from typer.testing import CliRunner

    from app.cli import app
    from app.models import Recap

    w = world
    s = transkribiert(client, w, dbs, tmp_path)
    r = CliRunner().invoke(app, ["recap-probe"])
    assert s["id"] in r.output
    assert "Kein API-Schlüssel" in CliRunner().invoke(app, ["recap-probe", s["id"]]).output
    einstellen(dbs, art="aus", api_key="sk-test-schluessel-1234")
    r = CliRunner().invoke(app, ["recap-probe", s["id"], "--modell", "mistral-small-latest"])
    assert r.exit_code == 0, r.output
    assert "Die Gruppe ritt nach Rabenfels." in r.output and "2 Aufrufe" in r.output
    assert api.aufrufe[0]["body"]["model"] == "mistral-small-latest"
    assert dbs.query(Recap).count() == 0
