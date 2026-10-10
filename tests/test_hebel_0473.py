"""Server 0.4.73, Weg „Notizen zuerst“: Pflichtprüfung mit nachgerechnetem Zitat, „unklar statt raten“, Regelreste
systemneutral, Länge vorab geplant, Auswahl nach Dauer, eigenes Modell für Notizen und Auswahl."""
import json
import re

import httpx
import pytest

from app import artefakte
from app import sprachmodell as sm
from tests.test_notizen_zuerst import _eingabe
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

KAPITEL = ("Mara floh aus der Stadt und ließ das Schwert Eisenwind zurück.\n\n"
           "Am Hafen ging Hanna Kessler über Bord. Jemand zog sie zurück ins Boot.")
PUNKTE = [{"zeit": 60.0, "ereignis": "Mara lässt das Schwert zurück.", "ausgang": "Eisenwind bleibt in der Stadt.",
           "rang": "kritisch"},
          {"zeit": 120.0, "ereignis": "Hanna geht über Bord und verliert das Silber.",
           "ausgang": "Das Silber ist weg.", "rang": "kritisch"},
          {"zeit": 130.0, "ereignis": "Hanna wird gerettet.", "ausgang": "Hanna lebt (unklar, wer).", "rang": "kritisch"}]


def test_pflicht_zitat_wird_nachgerechnet():
    """„erzählt“ ohne auffindbares Zitat ist „fehlt“; ein falscher Absatz wird nach dem Zitat berichtigt;
    „widerspricht“ ohne Zitat und ohne erkennbares Ereignis im Absatz gilt als ungeprüft (0.4.76, vorher „unklar“)."""
    d = {"punkte": [
        {"nr": 1, "status": "erzaehlt", "absatz": 2, "zitat": "ließ das Schwert Eisenwind zurück"},
        {"nr": 2, "status": "erzaehlt", "absatz": 2, "zitat": "Hanna verlor das Silber"},  # erfunden
        {"nr": 3, "status": "widerspricht", "absatz": 2, "zitat": "Mara zog sie zurück", "begruendung": "Es war nicht Mara."}]}
    aus = sm.pflicht_lesen(d, 3, 2, KAPITEL)
    assert aus[0]["status"] == "erzaehlt" and aus[0]["absatz"] == 0  # Zitat steht in Absatz 1
    assert aus[1]["status"] == "fehlt" and aus[1]["zitat"] == "" and aus[1]["begruendung"] == sm.OHNE_ZITAT["de"]
    assert aus[2]["status"] == "ungeprueft"
    # ohne Kapitel (ältere Aufrufer) bleibt alles wie vom Modell gemeldet
    assert [p["status"] for p in sm.pflicht_lesen(d, 3, 2)] == ["erzaehlt", "erzaehlt", "widerspricht"]
    assert "zitat" in sm.SYSTEM_PFLICHT and "findest du keine, ist der Status \"fehlt\"" in sm.SYSTEM_PFLICHT
    assert "gib den Absatz" in sm.SYSTEM_PFLICHT_NACH


def test_unklar_nicht_nachgebessert():
    class Klient:
        modell = "m"
        aufrufe = 0

        def chat(self, system, nutzer):
            Klient.aufrufe += 1
            return sm.Antwort(json.dumps({"absaetze": []}), 1, 1)

    befund = [{"nr": 3, "status": "unklar", "absatz": 1, "begruendung": "", "zitat": ""}]
    ein = {"kampagne": "K", "session_nummer": 1, "personen": []}
    assert sm.Ablauf(Klient()).pflicht_nachbessern(ein, PUNKTE, befund, KAPITEL) is None and Klient.aufrufe == 0


def test_regelreste_und_unklar_marke():
    assert sm.regelreste("[1:57] Mara schlägt zu (39 von 50, Erfolg).") == "[1:57] Mara schlägt zu."
    assert sm.regelreste("Hanna springt ins Wasser (Schwimmwurf: 46 von 20, gescheitert).") == "Hanna springt ins Wasser."
    assert sm.regelreste("Mara wird getroffen (4 Trefferpunkte).") == "Mara wird getroffen."
    assert sm.regelreste("Kano: tot (seit [1:02:10]), Rita (Deckname), (Witz?)") == "Kano: tot (seit [1:02:10]), Rita (Deckname), (Witz?)"
    text, befunde = artefakte.kapitel("Mara verlor unwillkürlich 2 Magiepunkte und zitterte. Ihr Schwimmwurf scheiterte. "
                                      "Sie machte ihm einen Vorwurf. Jemand half ihr nach oben (unklar, wer).")
    assert text == "Sie machte ihm einen Vorwurf. Jemand half ihr nach oben."
    assert [b["art"] for b in befunde] == ["regel", "regel"]
    u = sm.unklar_stellen("[2:20:04] Jemand packt Hanna an den Schultern (unklar, wer).\n[2:21] Der Motor springt an.")
    assert u == [{"zeit": 8404.0, "zeit_text": "2:20:04", "notiz": "Jemand packt Hanna an den Schultern (unklar, wer)."}]
    for prompt in (sm.SYSTEM_NOTIZEN, sm.ZUSATZ_STAND, sm.SYSTEM_AUSWAHL, sm.SYSTEM_RECAP):
        assert "unklar" in prompt


def test_auswahl_nach_dauer_und_regelreste():
    kurz = {"transkript": [{"start": 50 * 60.0}]}
    mittel = {"transkript": [{"start": 90 * 60.0}]}
    lang = {"transkript": [{"start": 155 * 60.0}]}
    assert (sm.auswahl_hoechstens(kurz), sm.auswahl_hoechstens(mittel), sm.auswahl_hoechstens(lang)) == (12, 20, 30)
    roh = {"ereignisse": [{"zeit": f"{i}:00", "ereignis": f"Ereignis {i} (3 Trefferpunkte)", "ausgang": "offen (Probe gelungen)",
                           "rang": "kritisch"} for i in range(40)]}
    aus = sm.auswahl_lesen(roh, 30)
    assert len(aus) == 30 and aus[0]["ereignis"] == "Ereignis 0" and aus[0]["ausgang"] == "offen"
    assert len(sm.auswahl_lesen(roh)) == sm.AUSWAHL_HOECHSTENS


class Anbieter:
    """Notizen mit einer unklaren Stelle und einem Regelrest; Pflichtprüfung meldet ein erfundenes Zitat."""

    def __init__(self):
        self.aufrufe = []
        self.modelle = []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Mara wird getroffen (4 Trefferpunkte).",
                                f"[{zeiten[-1]}] Jemand zieht Hanna aus dem Wasser (unklar, wer)."],
                    "stand": ["Hanna: gerettet (unklar, von wem)"]}
        if system.startswith("Du bereitest das Kapitel"):
            return {"ereignisse": [
                {"zeit": "2:00", "ereignis": "Hanna wird aus dem Wasser gezogen.", "ausgang": "gerettet (unklar, wer)",
                 "rang": "kritisch"},
                {"zeit": "1:00", "ereignis": "Mara wird getroffen.", "ausgang": "verletzt", "rang": "wichtig"}]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": "Mara wurde getroffen (unklar, wer).\n\nJemand zog Hanna aus dem Wasser.",
                    "openThreads": []}
        if system.startswith("Du vergleichst das Kapitel"):
            return {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "Mara wurde getroffen"},
                               {"nr": 2, "status": "erzaehlt", "absatz": 2, "zitat": "Hanna wurde von Mara gerettet"}]}
        if system.startswith("Du überarbeitest einzelne Absätze im Kapitel"):
            return {"absaetze": [{"nr": 2, "text": "Jemand zog Hanna aus dem Wasser, sie lebte."}]}
        if "Du prüfst den Recap" in system:
            return {"absaetze": [{"nr": 1, "urteil": "belegt"}, {"nr": 2, "urteil": "belegt"}]}
        if "Kampagnen-Bibel" in system:
            return {"proposals": []}
        return {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, nutzer = body["messages"][0]["content"], body["messages"][1]["content"]
        self.aufrufe.append({"system": system, "nutzer": nutzer, "modell": body.get("model")})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.antwort(system, nutzer),
                                                                                         ensure_ascii=False)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def test_ablauf_0473(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    http = httpx.Client(transport=httpx.MockTransport(anbieter))
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "medium", http)
    notiz = sm.OpenAIKlient("https://llm.example/v1", "sk", "large", http)
    a = sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=2000,
                  notiz_klient=notiz)
    d = a.ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    # Notizen und Auswahl mit dem Notizen-Modell, Kapitel und Prüfung mit dem Kapitel-Modell
    def modell(anfang: str) -> str:
        return next(x["modell"] for x in anbieter.aufrufe if x["system"].startswith(anfang))

    assert modell("Du hilfst bei der Nachbereitung") == "large" and modell("Du bereitest das Kapitel") == "large"
    assert modell("Du schreibst den Recap") == "medium" and modell("Du vergleichst das Kapitel") == "medium"
    # Regelrest aus der Notiz entfernt, unklare Stelle gesammelt
    assert "(4 Trefferpunkte)" not in a.letzte_notizen and "Mara wird getroffen." in a.letzte_notizen
    assert len(a.letzte_unklar) == 1 and "unklar, wer" in a.letzte_unklar[0]["notiz"]
    assert a.letzte_auswahl[1].get("unklar") is True and a.letzte_auswahl[1]["ausgang"] == "gerettet (unklar, wer)"
    # Auswahl nach Dauer (150 min → 30) und geplante Länge im Prompt
    auswahl = next(x for x in anbieter.aufrufe if x["system"].startswith("Du bereitest das Kapitel"))
    assert "höchstens 30" in auswahl["system"]
    recap = next(x for x in anbieter.aufrufe if x["system"].startswith("Du schreibst den Recap"))
    assert re.search(r"Etwa \d+ Absätze mit zusammen 900 bis 1500 Wörtern", recap["system"])
    # Die Marke steht nicht im Kapitel; das erfundene Zitat macht Punkt 2 zu „fehlt“ → Nachbesserung
    assert "(unklar" not in d["text"] and d["text"].startswith("Mara wurde getroffen.")
    assert a.letzte_pflicht["vorher"][1]["status"] == "fehlt" and a.letzte_pflicht["nachgebessert"] is True
    assert d["text"].endswith("Jemand zog Hanna aus dem Wasser, sie lebte.")


def test_einstellung_notizenmodell(client, dbs, admin):  # noqa: F811
    from app.einstellungen import llm_konfig
    from app.zusammenfassung import api_klient_notizen

    daten = {"csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest",
             "api_modell_notizen": "mistral-large-latest", "lokal_modell": "auto", "lokal_kontext": "12288",
             "api_key": "sk-abcdefghijklmnop1234"}
    client.post("/verwaltung/zusammenfassung", data=daten)
    dbs.expire_all()
    k = llm_konfig(dbs)
    assert k.api_modell_notizen == "mistral-large-latest" and api_klient_notizen(k).modell == "mistral-large-latest"
    seite = client.get("/verwaltung/zusammenfassung").text
    assert 'name="api_modell_notizen"' in seite and "Modell für Notizen und Auswahl" in seite
    client.post("/verwaltung/zusammenfassung", data={**daten, "api_modell_notizen": "mistral-medium-latest"})
    dbs.expire_all()
    assert llm_konfig(dbs).api_modell_notizen == "" and api_klient_notizen(llm_konfig(dbs)) is None


def test_probelauf_dateien_0473():
    from app import kapitelprobe

    assert "unklar.json" in kapitelprobe.DATEIEN
