"""Server 0.4.63: Weg „Notizen zuerst“ (nur im Probelauf wählbar) – Abschnitte nacheinander mit laufendem Stand und
Überlappung als Lesekontext, Stand am Ende für das Kapitel, Vorschläge weiter aus der ganzen Abschrift, mehr Platz für
lange Runden."""
import json
import re
import time

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")


class Anbieter:
    def __init__(self):
        self.aufrufe = []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            n = len([a for a in self.aufrufe if "Schreibe Szenennotizen" in a["system"]])
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Ereignis in Abschnitt {n} mit Folgen für die Gruppe.",
                                f"[{zeiten[-1]}] Ende von Abschnitt {n}, alle ziehen weiter."],
                    "stand": [f"Kano: tot (seit [{zeiten[0]}])"] if n >= 2 else ["Kano: gefangen"]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": "Die Gruppe ritt los.", "openThreads": []}
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


def _eingabe(client, world, dbs, tmp_path):
    from app.models import GameSession
    from app.zusammenfassung import eingabe_bauen, recap_eingabe, vorschlag_eingabe

    s = transkribiert(client, world, dbs, tmp_path)
    basis = eingabe_bauen(dbs, dbs.get(GameSession, s["id"]))
    recap_ein, vorschlag_ein = recap_eingabe(basis), vorschlag_eingabe(dbs, dbs.get(GameSession, s["id"]), basis)
    # 150 Minuten, eine Zeile je Minute, abwechselnde Sprecher
    zeilen = [{"start": m * 60.0, "sprecher": "Anna" if m % 2 else "Ben",
               "text": f"Zeile {m}: " + "Die Gruppe zieht weiter durch die Stadt und redet. " * 4} for m in range(150)]
    recap_ein["transkript"] = vorschlag_ein["transkript"] = zeilen
    return recap_ein, vorschlag_ein


def test_notizen_zuerst_mit_stand_und_ueberlappung(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    ablauf = sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=2000)
    d = ablauf.ausfuehren(recap_ein, vorschlag_ein)
    notizaufrufe = [a for a in anbieter.aufrufe if "Schreibe Szenennotizen" in a["system"]]
    assert len(notizaufrufe) >= 3
    assert "„stand“" not in notizaufrufe[0]["system"] and '"stand"' in notizaufrufe[0]["system"]
    # Abschnitt 1 ohne Vorgeschichte, ab Abschnitt 2 mit Stand und Lesekontext aus den letzten 2 Minuten davor
    assert "Stand bisher" not in notizaufrufe[0]["nutzer"] and "Unmittelbar davor" not in notizaufrufe[0]["nutzer"]
    zweiter = notizaufrufe[1]["nutzer"]
    assert "Stand bisher:\n- Kano: gefangen" in zweiter
    davor = zweiter.split("Unmittelbar davor (nur zum Verständnis, dazu keine Notizen):\n")[1].split("\n\nAbschnitt")[0]
    assert 1 <= len(davor.split("\n")) <= 3  # Zeilen je Minute → höchstens die letzten 2 Minuten
    eigene = zweiter.split("des Transkripts:\n")[1]
    assert davor.split("\n")[-1] not in eigene  # der Lesekontext gehört nicht zum Abschnitt selbst
    # Stand mit Zeitpunkt der Änderung geht ans Kapitel; die Vorschläge sehen die ganze Abschrift
    assert ablauf.letzter_stand[0].startswith("Kano: tot (seit [")
    recap = next(a for a in anbieter.aufrufe if a["system"].startswith("Du schreibst den Recap"))
    assert "Stand am Ende der Runde" in recap["nutzer"] and "Kano: tot" in recap["nutzer"]
    assert "900–1500" in recap["system"]
    vorschlag = next(a for a in anbieter.aufrufe if "Kampagnen-Bibel" in a["system"])
    assert "\nTranskript:\n" in vorschlag["nutzer"] and "Zeile 149:" in vorschlag["nutzer"]
    assert ablauf.letzte_notizen.count("Ereignis in Abschnitt") == len(notizaufrufe)
    assert all("Abschnitt" in a["nutzer"] for a in notizaufrufe)
    assert d["text"] == "Die Gruppe ritt los."


def test_ohne_schalter_bleibt_alles_wie_bisher(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False).ausfuehren(recap_ein, vorschlag_ein)
    assert not any("Schreibe Szenennotizen" in a["system"] for a in anbieter.aufrufe)
    recap = next(a for a in anbieter.aufrufe if a["system"].startswith("Du schreibst den Recap"))
    assert "600–1200" in recap["system"] and "Stand am Ende" not in recap["nutzer"]


def test_probelauf_mit_weg(client, world, dbs, tmp_path, admin):  # noqa: F811
    from app import kapitelprobe as probelauf
    from app.models import Campaign, Member, User
    from tests.test_step5 import einstellen

    s = transkribiert(client, world, dbs, tmp_path)
    einstellen(dbs, art="attrappe")
    chef = dbs.query(User).filter_by(username="chef").one()
    dbs.add(Member(campaign_id=world["cid"], user_id=chef.id, role="gm"))
    dbs.get(Campaign, world["cid"]).allow_cloud_summary = True
    dbs.commit()
    seite = client.get("/verwaltung/zusammenfassung").text
    assert 'name="weg" value="notizen"' in seite
    r = client.post("/verwaltung/probelauf", data={"csrf": admin, "session": s["id"], "weg": "notizen"},
                    follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    assert probelauf.lesen(pid).weg == "notizen"
    for _ in range(100):
        if probelauf.lesen(pid).zustand != "läuft":
            break
        time.sleep(0.05)
    assert "Erst Notizen, dann Kapitel" in client.get(f"/verwaltung/probelauf/{pid}").text
    r = client.post("/verwaltung/probelauf", data={"csrf": admin, "session": s["id"], "weg": "unsinn"},
                    follow_redirects=False)
    assert probelauf.lesen(r.headers["location"].rsplit("/", 1)[1]).weg == "abschrift"
    assert "notizen.txt" in probelauf.DATEIEN and "stand.txt" in probelauf.DATEIEN
