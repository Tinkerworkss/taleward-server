"""Server 0.4.65: Weg „Notizen zuerst“ – erst die entscheidenden Ereignisse auswählen, dann das Kapitel schreiben, dann
gegen diese Auswahl prüfen und genau einmal nachbessern. Dazu eine schärfere Gegenprüfung."""
import json
import re

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_notizen_zuerst import _eingabe
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

ALT = "Die Gruppe floh aus der Stadt."
FALSCH = "Mara starb in den Trümmern, und die Gruppe nahm ihr Schwert."
RICHTIG = "Mara überlebte verletzt in den Trümmern und gab der Gruppe ihr Schwert."


class Anbieter:
    def __init__(self, bleibt_falsch: bool = False):
        self.aufrufe = []
        self.bleibt_falsch = bleibt_falsch

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Mara wird verschüttet.", f"[{zeiten[-1]}] Mara überlebt."],
                    "stand": ["Mara: verletzt, lebt"]}
        if system.startswith("Du bereitest das Kapitel"):
            return {"ereignisse": [
                {"zeit": "2:00", "ereignis": "Mara wird verschüttet und gibt der Gruppe ihr Schwert.",
                 "ausgang": "Mara überlebt verletzt.", "rang": "kritisch"},
                {"zeit": "1:00", "ereignis": "Die Gruppe flieht aus der Stadt.", "ausgang": "", "rang": "wichtig"},
                {"zeit": "3:00", "ereignis": "Die Spielleitung erklärt die Regeln.", "ausgang": "", "rang": "wichtig"}]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": f"{ALT}\n\n{FALSCH}", "openThreads": []}
        if system.startswith("Du vergleichst das Kapitel"):
            zweite = [a for a in self.aufrufe if a["system"].startswith("Du vergleichst das Kapitel")]
            falsch = len(zweite) == 1 or self.bleibt_falsch
            return {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "floh aus der Stadt"},
                               {"nr": 2, "status": "widerspricht" if falsch else "erzaehlt", "absatz": 2,
                                "zitat": "Mara starb in den Trümmern" if falsch else "Mara überlebte verletzt",
                                "begruendung": "Mara überlebt, sie stirbt nicht." if falsch else ""}]}
        if system.startswith("Du überarbeitest einzelne Absätze im Kapitel"):
            return {"absaetze": [{"nr": 2, "text": FALSCH if self.bleibt_falsch else RICHTIG}]}
        if "Du prüfst den Recap" in system:
            return {"absaetze": [{"nr": 1, "urteil": "belegt"}, {"nr": 2, "urteil": "belegt"}]}
        if "Kampagnen-Bibel" in system:
            return {"proposals": []}
        return {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, nutzer = body["messages"][0]["content"], body["messages"][1]["content"]
        self.aufrufe.append({"system": system, "nutzer": nutzer})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.antwort(system, nutzer),
                                                                                         ensure_ascii=False)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def _ablauf(anbieter, **kw):
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    return sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=2000, **kw)


def test_auswahl_pflicht_und_nachbesserung(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    a = _ablauf(anbieter)
    d = a.ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    systeme = [x["system"][:40] for x in anbieter.aufrufe]
    # Auswahl sieht Notizen und Stand; Spielleitung fällt heraus; nach Zeit sortiert
    auswahl = next(x for x in anbieter.aufrufe if x["system"].startswith("Du bereitest das Kapitel"))
    assert "Stand am Ende der Runde" in auswahl["nutzer"] and "Mara: verletzt, lebt" in auswahl["nutzer"]
    assert [p["zeit"] for p in a.letzte_auswahl] == [60.0, 120.0]
    # Das Kapitel bekommt die Pflichtpunkte mit Ausgang
    recap = next(x for x in anbieter.aufrufe if x["system"].startswith("Du schreibst den Recap"))
    assert "müssen im Kapitel vorkommen" in recap["nutzer"] and "Ausgang: Mara überlebt verletzt." in recap["nutzer"]
    # statt der allgemeinen Vollständigkeitsprüfung: Pflichtprüfung, Nachbesserung, zweite Pflichtprüfung
    assert not any(s.startswith("Du vergleichst den Recap") for s in systeme)
    assert sum(s.startswith("Du vergleichst das Kapitel") for s in systeme) == 2
    assert a.letzte_pflicht["nachgebessert"] is True and a.letztes_kapitel1 == f"{ALT}\n\n{FALSCH}"
    assert d["text"] == f"{ALT}\n\n{RICHTIG}"
    assert [p["verdict"] for p in d["review"]["paragraphs"]] == ["supported", "supported"]
    # nur der betroffene Absatz geht an die Nachbesserung
    nach = next(x for x in anbieter.aufrufe if x["system"].startswith("Du überarbeitest einzelne Absätze im Kapitel"))
    assert "Absatz 2:" in nach["nutzer"] and "Absatz 1:" not in nach["nutzer"]
    assert "richtiger Ausgang: Mara überlebt verletzt." in nach["nutzer"]


def test_bleibt_falsch_wird_markiert(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    d = _ablauf(Anbieter(bleibt_falsch=True)).ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    p = d["review"]["paragraphs"]
    assert p[0]["verdict"] == "supported"
    assert p[1]["verdict"] == "contradicted" and p[1]["note"] == "Mara überlebt, sie stirbt nicht."


def test_ohne_notizen_weg_keine_auswahl(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False).ausfuehren(recap_ein, vorschlag_ein)
    assert not any(x["system"].startswith(("Du bereitest das Kapitel", "Du vergleichst das Kapitel"))
                   for x in anbieter.aufrufe)


def test_nachbesserung_darf_bei_fehlendem_nicht_kuerzen():
    class Klient:
        modell = "m"

        def chat(self, system, nutzer):
            return sm.Antwort(json.dumps({"absaetze": [{"nr": 1, "text": "Kurz."}]}), 1, 1)

    punkte = [{"zeit": 60.0, "ereignis": "Mara gibt das Schwert ab.", "ausgang": "", "rang": "kritisch"}]
    befund = [{"nr": 1, "status": "fehlt", "absatz": 0, "begruendung": "fehlt"}]
    ein = {"kampagne": "K", "session_nummer": 1, "personen": []}
    assert sm.Ablauf(Klient()).pflicht_nachbessern(ein, punkte, befund, "Ein längerer Absatz ohne das Schwert.") is None


def test_auswahl_lesen_begrenzt_und_sortiert():
    roh = {"ereignisse": [{"zeit": f"{60 - i}:00", "ereignis": f"Ereignis {i}", "rang": "critical"} for i in range(30)]}
    roh["ereignisse"].append({"zeit": "1:00", "ereignis": "Ereignis 1"})  # doppelt
    aus = sm.auswahl_lesen(roh)
    assert len(aus) == sm.AUSWAHL_HOECHSTENS and aus[0]["zeit"] < aus[-1]["zeit"]
    assert all(a["rang"] == "kritisch" for a in aus)


def test_pruefung_schaerfer():
    from app import pruefteil
    from app.pruefteil import Abschnitt, Stellen

    assert 'nie "teilweise"' in sm.SYSTEM_PRUEFUNG and "zwei Figuren werden zu einer" in sm.SYSTEM_PRUEFUNG
    stellen = Stellen([Abschnitt(10.0, 20.0, "Pipo lebt.", None)])
    roh = {"paragraphs": [
        {"index": 0, "verdict": "partial", "note": "Pipo stirbt nicht, sondern überlebt.", "evidence": []},
        {"index": 1, "verdict": "partial", "note": None, "evidence": []}]}
    p = pruefteil.bauen(roh, "Pipo starb.\n\nDie Gruppe zog weiter.", stellen)["paragraphs"]
    assert p[0]["verdict"] == "contradicted" and p[1]["verdict"] == "partial"


def test_probelauf_dateien():
    from app import kapitelprobe

    assert {"auswahl.txt", "pflicht.json", "entwurf.txt"} <= set(kapitelprobe.DATEIEN)
