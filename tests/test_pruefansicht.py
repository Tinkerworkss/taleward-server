"""Server 0.4.75, Weg „Notizen zuerst“: Prüfansicht aus Pflichtprüfung und unklaren Stellen statt Gegenprüfung jedes
Absatzes gegen die Notizen. Nur Nachgerechnetes und Gekennzeichnetes wird markiert; die alte Gegenprüfung bleibt
wählbar."""
import json
import re

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_notizen_zuerst import _eingabe
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

TEXT = ("Mara floh aus der Stadt.\n\nAm Hafen half jemand Hanna aus dem Wasser.\n\n"
        "Kano fiel in den Trümmern.\n\nAbends saßen alle am Feuer.")
PUNKTE = [{"zeit": 60.0, "ereignis": "Mara flieht aus der Stadt.", "ausgang": "", "rang": "wichtig"},
          {"zeit": 120.0, "ereignis": "Jemand hilft Hanna aus dem Wasser.", "ausgang": "gerettet (unklar: wer)",
           "rang": "kritisch", "unklar": True},
          {"zeit": 180.0, "ereignis": "Kano stirbt in den Trümmern.", "ausgang": "tot", "rang": "kritisch"},
          {"zeit": 200.0, "ereignis": "Die Gruppe verliert das Silber.", "ausgang": "Silber weg", "rang": "kritisch"}]
BEFUND = [{"nr": 1, "status": "erzaehlt", "absatz": 0, "zitat": "Mara floh aus der Stadt", "begruendung": ""},
          {"nr": 2, "status": "erzaehlt", "absatz": 1, "zitat": "half jemand Hanna aus dem Wasser", "begruendung": ""},
          {"nr": 3, "status": "widerspricht", "absatz": 2, "zitat": "Kano fiel in den Trümmern",
           "begruendung": "Kano stirbt, er fällt nicht nur."},
          {"nr": 4, "status": "fehlt", "absatz": 2, "zitat": "", "begruendung": "Kein Satz im Kapitel erzählt diesen Ausgang."}]


def test_pruefansicht_aus_pflicht():
    blick = [{"nr": 2, "urteil": "unklar", "zitat": ""}, {"nr": 3, "urteil": "stimmt", "zitat": "Kano ist tot, sagt die SL"}]
    p = sm.pruefansicht_aus_pflicht(TEXT, PUNKTE, BEFUND, [], blick, "m", True)
    v = {a["index"]: a for a in p["paragraphs"]}
    assert p["model"] == "m" and p["revised"] is True and len(v) == 4
    assert v[0]["verdict"] == "supported" and v[0]["note"] is None and v[0]["evidence"] == [{"start": 60.0, "quote": ""}]
    assert v[1]["verdict"] == "unsupported" and v[1]["note"] == "Die Aufnahme ist hier nicht eindeutig: Jemand hilft Hanna aus dem Wasser. – wer?"
    # widerspricht schlägt fehlt; Beleg aus dem zweiten Blick wird zum Zitat
    assert v[2]["verdict"] == "contradicted" and v[2]["note"] == "Kano stirbt, er fällt nicht nur."
    assert {"start": 180.0, "quote": "Kano ist tot, sagt die SL"} in v[2]["evidence"]
    assert v[3]["verdict"] == "unchecked" and v[3]["evidence"] == []
    # nur fehlt → partial mit „Nicht erzählt“
    befund = [dict(b, status="erzaehlt") for b in BEFUND[:3]] + [BEFUND[3]]
    p2 = sm.pruefansicht_aus_pflicht(TEXT, PUNKTE[:1] + [dict(PUNKTE[1], unklar=False, ausgang="gerettet")] + PUNKTE[2:],
                                     befund, [], [], "m", False)
    assert p2["paragraphs"][2]["verdict"] == "partial" and p2["paragraphs"][2]["note"] == "Nicht erzählt: Die Gruppe verliert das Silber. – Silber weg"
    # unklare Notiz ohne eigenes Ereignis: zum Absatz des nächsten Ereignisses
    p3 = sm.pruefansicht_aus_pflicht(TEXT, PUNKTE[:1], BEFUND[:1], [{"zeit": 70.0, "notiz": "Jemand ruft (unklar, wer)."}],
                                     [], "m", False)
    assert p3["paragraphs"][0]["verdict"] == "unsupported" and p3["paragraphs"][0]["note"].endswith("Jemand ruft.")
    # Zitat verortet den Absatz neu, wenn die angegebene Nummer nicht passt
    p4 = sm.pruefansicht_aus_pflicht(TEXT, PUNKTE[:1], [dict(BEFUND[0], absatz=3)], [], [], "m", False)
    assert p4["paragraphs"][0]["verdict"] == "supported" and p4["paragraphs"][3]["verdict"] == "unchecked"


class Anbieter:
    def __init__(self):
        self.aufrufe = []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Mara flieht aus der Stadt."], "stand": []}
        if system.startswith("Du bereitest das Kapitel"):
            return {"ereignisse": [{"zeit": "1:00", "ereignis": "Mara flieht aus der Stadt.", "ausgang": "", "rang": "kritisch"}]}
        if system.startswith("Du prüfst einzelne Ereignisse"):
            return {"ereignisse": [{"nr": 1, "urteil": "stimmt"}]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": "Mara floh aus der Stadt.\n\nAbends saßen alle am Feuer.", "openThreads": []}
        if system.startswith("Du vergleichst das Kapitel"):
            return {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "Mara floh aus der Stadt"}]}
        if "Du prüfst den Recap" in system:
            return {"absaetze": [{"nr": 1, "urteil": "belegt"}, {"nr": 2, "urteil": "unbelegt", "begruendung": "Feuer fehlt."}]}
        if "Kampagnen-Bibel" in system:
            return {"proposals": []}
        return {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, nutzer = body["messages"][0]["content"], body["messages"][1]["content"]
        self.aufrufe.append(system[:30])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.antwort(system, nutzer),
                                                                                         ensure_ascii=False)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def _ablauf(anbieter, **kw):
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    return sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=2000, **kw)


def test_im_ablauf_ohne_gegenpruefung(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    d = _ablauf(anbieter).ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    assert not any(s.startswith("Du prüfst den Recap") for s in anbieter.aufrufe)  # keine Gegenprüfung mehr
    assert not any(s.startswith("Du prüfst einzelne Absätze") for s in anbieter.aufrufe)  # keine Relationsprüfung
    p = d["review"]["paragraphs"]
    assert [a["verdict"] for a in p] == ["supported", "unchecked"] and p[0]["evidence"][0]["start"] == 60.0
    assert d["review"]["revised"] is False
    # alter Weg weiter wählbar
    anbieter2 = Anbieter()
    d2 = _ablauf(anbieter2, pruefansicht="gegenpruefung").ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    assert any(s.startswith("Du prüfst den Recap") for s in anbieter2.aufrufe)
    assert d2["review"]["paragraphs"][1]["verdict"] == "unsupported"
    # Gegenprüfung in der Verwaltung ganz aus: keine Prüfansicht
    assert "review" not in _ablauf(Anbieter()).ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=False)


def test_einstellung_pruefansicht(client, dbs, admin):  # noqa: F811
    from app.zusammenfassung import pruefansicht_art

    assert pruefansicht_art(dbs) == "pflicht"
    seite = client.get("/verwaltung/zusammenfassung").text
    assert 'name="pruefansicht" value="pflicht" checked' in seite
    daten = {"csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest",
             "lokal_modell": "auto", "lokal_kontext": "12288", "api_key": "sk-abcdefghijklmnop1234",
             "gegenpruefen_feld": "1", "gegenpruefen": "1", "zweiter_blick": "1"}
    client.post("/verwaltung/zusammenfassung", data={**daten, "pruefansicht": "gegenpruefung"})
    dbs.expire_all()
    assert pruefansicht_art(dbs) == "gegenpruefung"
    client.post("/verwaltung/zusammenfassung", data={**daten, "pruefansicht": "unsinn"})
    dbs.expire_all()
    assert pruefansicht_art(dbs) == "gegenpruefung"
    client.post("/verwaltung/zusammenfassung", data={**daten, "pruefansicht": "pflicht"})
    dbs.expire_all()
    assert pruefansicht_art(dbs) == "pflicht"
