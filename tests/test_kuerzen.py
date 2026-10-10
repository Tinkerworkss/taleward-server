"""Server 0.4.68, Weg „Notizen zuerst“: Zusagen nur, wenn ausdrücklich; Pflichtprüfung ohne reine Zahlenabweichungen;
ein zu langes Kapitel wird nur dann gekürzt übernommen, wenn dabei nichts verloren geht."""
import json
import re

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_notizen_zuerst import _eingabe
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

_SAETZE = [f"{a} {n} {v} still." for a in ("Alte", "Graue", "Nasse", "Kalte", "Dunkle", "Stille", "Leere", "Schmale")
           for n in ("Laternen", "Mauern", "Gassen", "Dächer", "Fenster", "Brücken", "Türme", "Höfe", "Boote", "Wagen")
           for v in ("flackerten", "glänzten", "schwiegen", "warteten", "knarrten", "tropften")]
SCHMUCK = " ".join(_SAETZE[:260])  # ≈ 1040 Wörter Ausschmückung
REST = " ".join(_SAETZE[260:460])  # ≈ 800 Wörter, bleiben in der Kurzfassung
LANG = (f"Mara traf Hanna Kessler am Hafen und gab ihr das Schwert Eisenwind. {SCHMUCK}\n\n"
        f"Später floh die Gruppe aus der Stadt. {REST}")
KURZ = f"Mara traf Hanna Kessler am Hafen und gab ihr das Schwert Eisenwind.\n\nSpäter floh die Gruppe aus der Stadt. {REST}"
OHNE_NAME = f"Mara traf eine Frau am Hafen und gab ihr das Schwert.\n\nSpäter floh die Gruppe aus der Stadt. {REST}"


class Anbieter:
    def __init__(self, kurz: str = KURZ, lang: str = LANG, verliert: bool = False):
        self.aufrufe, self.kurz, self.lang, self.verliert = [], kurz, lang, verliert

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Mara gibt Hanna Kessler das Schwert Eisenwind."], "stand": []}
        if system.startswith("Du bereitest das Kapitel"):
            return {"ereignisse": [
                {"zeit": "1:00", "ereignis": "Mara gibt Hanna Kessler das Schwert Eisenwind.", "ausgang": "",
                 "rang": "kritisch"},
                {"zeit": "2:00", "ereignis": "Die Gruppe flieht aus der Stadt.", "ausgang": "", "rang": "wichtig"}]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": self.lang, "openThreads": []}
        if system.startswith("Du kürzt das Kapitel"):
            return {"text": self.kurz}
        if system.startswith("Du vergleichst das Kapitel"):
            gekuerzt = "Alte Laternen flackerten" not in nutzer
            zweiter = "fehlt" if (gekuerzt and self.verliert) else "erzaehlt"
            return {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "gab ihr das Schwert Eisenwind"},
                               {"nr": 2, "status": zweiter, "absatz": 2, "begruendung": "",
                                "zitat": "" if zweiter == "fehlt" else "floh die Gruppe aus der Stadt"}]}
        if "Du prüfst den Recap" in system:
            return {"absaetze": [{"nr": 1, "urteil": "belegt"}, {"nr": 2, "urteil": "belegt"}]}
        return {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, nutzer = body["messages"][0]["content"], body["messages"][1]["content"]
        self.aufrufe.append(system[:40])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.antwort(system, nutzer),
                                                                                         ensure_ascii=False)}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


def _lauf(anbieter, client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    a = sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=2000)
    return a, a.ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)


def test_kuerzung_ohne_verlust_wird_uebernommen(client, world, dbs, tmp_path):
    a, d = _lauf(Anbieter(), client, world, dbs, tmp_path)
    assert "Alte Laternen flackerten" not in d["text"] and "Hanna Kessler" in d["text"] and a.letztes_lang
    assert a.letzte_kuerzung["angenommen"] is True and a.letzte_pflicht["kuerzung"]["woerter_neu"] < 900


def test_kuerzung_ohne_namen_wird_verworfen(client, world, dbs, tmp_path):
    a, d = _lauf(Anbieter(kurz=OHNE_NAME), client, world, dbs, tmp_path)
    assert "Alte Laternen flackerten" in d["text"] and a.letztes_lang == ""
    assert a.letzte_kuerzung["angenommen"] is False and "Namen fehlen" in a.letzte_kuerzung["grund"]
    assert "kessl" in a.letzte_kuerzung["grund"] and "eisen" in a.letzte_kuerzung["grund"]


def test_kuerzung_mit_verlorenem_pflichtpunkt_wird_verworfen(client, world, dbs, tmp_path):
    a, d = _lauf(Anbieter(verliert=True), client, world, dbs, tmp_path)
    assert "Alte Laternen flackerten" in d["text"] and a.letzte_kuerzung["grund"] == "Pflichtpunkte verloren: 2"


def test_kurzes_kapitel_bleibt_ohne_aufruf(client, world, dbs, tmp_path):
    anbieter = Anbieter(lang=KURZ)  # ≈ 830 Wörter, unter der Grenze
    a, d = _lauf(anbieter, client, world, dbs, tmp_path)
    assert "Alte Laternen flackerten" not in d["text"] and not any(s.startswith("Du kürzt") for s in anbieter.aufrufe)
    assert a.letzte_kuerzung == {"woerter": len(KURZ.split()), "grenze": 1500,
                                 "angenommen": False, "grund": "nicht zu lang"}


def test_regeln_fuer_zusagen_und_zahlen():
    assert "keine Zusage" in sm.ZUSATZ_STAND and "gibt sich als Y aus" in sm.ZUSATZ_STAND
    assert "Antwort offen, dann" in sm.SYSTEM_AUSWAHL and "Decknamen" in sm.SYSTEM_AUSWAHL
    assert "kein Widerspruch" in sm.SYSTEM_PFLICHT and "nicht zusätzlich stehen" in sm.SYSTEM_PFLICHT_NACH
    assert "keine Zusage" not in sm.SYSTEM_NOTIZEN  # der normale Weg bleibt unverändert


def test_probelauf_datei_lang():
    from app import kapitelprobe

    assert "lang.txt" in kapitelprobe.DATEIEN
