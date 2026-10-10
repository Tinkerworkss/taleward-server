"""Server 0.4.74, Weg „Notizen zuerst“: Zweiter Blick in die Abschrift – kritische und unklare Ereignisse der Auswahl
werden vor dem Schreiben gegen den Wortlaut rund um ihren Zeitpunkt gehalten. Berichtigt nur mit auffindbarem Zitat,
sonst unklar (Kapitel bleibt vage)."""
import json
import re

import httpx
import pytest

from app import sprachmodell as sm
from tests.test_notizen_zuerst import _eingabe
from tests.test_step2b import _kleine_teile, transkribiert  # noqa: F401
from tests.test_verwaltung import admin  # noqa: F401

pytestmark = pytest.mark.usefixtures("_kleine_teile")

ZEILEN = [(float(s), f"[{s // 60}:{s % 60:02d}] {'Spielleitung' if (s // 20) % 2 else 'Mara'}: Zeile bei {s} Sekunden, "
                      + ("dann packt dich Hanna an den Schultern." if s == 600 else "die Gruppe redet."))
          for s in range(0, 1200, 20)]


def test_fenster_um_den_zeitpunkt():
    f = sm._fenster(ZEILEN, 600.0)
    zeiten = [int(re.match(r"\[(\d+):(\d+)\]", z).group(1)) * 60 + int(re.match(r"\[(\d+):(\d+)\]", z).group(2))
              for z in f.split("\n")]
    assert min(zeiten) == 520 and max(zeiten) == 680 and "packt dich Hanna" in f
    klein = sm._fenster(ZEILEN, 600.0, zeichen=200)
    assert 0 < len(klein) <= 200 + 80 and "packt dich Hanna" in klein  # von der Mitte her begrenzt
    assert sm._fenster(ZEILEN, 5000.0) == ""


class Klient:
    """Antwortet auf den zweiten Blick: 1 stimmt, 2 berichtigt mit echtem Zitat, 3 berichtigt mit erfundenem Zitat."""
    modell = "medium"

    def __init__(self):
        self.aufrufe = []

    def chat(self, system, nutzer):
        self.aufrufe.append(nutzer)
        return sm.Antwort(json.dumps({"ereignisse": [
            {"nr": 1, "urteil": "stimmt"},
            {"nr": 2, "urteil": "berichtigt", "ereignis": "Hanna packt Mara an den Schultern und hilft ihr nach oben.",
             "ausgang": "Mara ist gerettet.", "zitat": "dann packt dich Hanna an den Schultern"},
            {"nr": 3, "urteil": "berichtigt", "ereignis": "Kano stirbt.", "ausgang": "tot",
             "zitat": "Kano fällt tot um"}]}, ensure_ascii=False), 1000, 100)


def _punkte():
    return [{"zeit": 100.0, "ereignis": "Die Gruppe bricht auf.", "ausgang": "", "rang": "kritisch"},
            {"zeit": 600.0, "ereignis": "Mara packt Hanna an den Schultern und hilft ihr nach oben.",
             "ausgang": "Hanna ist gerettet.", "rang": "kritisch"},
            {"zeit": 900.0, "ereignis": "Kano wird verletzt.", "ausgang": "verletzt (unklar, wer)", "rang": "wichtig",
             "unklar": True},
            {"zeit": 1000.0, "ereignis": "Alle essen.", "ausgang": "", "rang": "wichtig"}]


def test_zweiter_blick_berichtigt_nur_mit_beleg():
    k = Klient()
    a = sm.Ablauf(k, notiz_klient=sm.Ablauf(Klient()).klient)
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [], "transkript": [
        {"start": t, "sprecher": z.split("] ")[1].split(":")[0], "text": z.split(": ", 1)[1]} for t, z in ZEILEN]}
    punkte = _punkte()
    protokoll = a.zweiter_blick(ein, punkte)
    # nur kritische und unklare Ereignisse, mit dem Kapitel-Modell, in einem Aufruf
    assert len(k.aufrufe) == 1 and "Ereignis 3" in k.aufrufe[0] and "Alle essen" not in k.aufrufe[0]
    assert "Abschrift dazu:" in k.aufrufe[0] and "packt dich Hanna" in k.aufrufe[0]
    assert [p["urteil"] for p in protokoll] == ["stimmt", "berichtigt", "unklar"]
    assert punkte[1]["ereignis"].startswith("Hanna packt Mara") and punkte[1]["ausgang"] == "Mara ist gerettet."
    assert punkte[1]["berichtigt"] is True and protokoll[1]["zitat"] == "dann packt dich Hanna an den Schultern"
    assert protokoll[1]["vorher"].startswith("Mara packt Hanna") and protokoll[1]["nachher"].startswith("Hanna packt Mara")
    # Berichtigung ohne Beleg: nicht übernommen, Ereignis bleibt unklar; war es schon unklar, bleibt die Marke einfach
    assert punkte[2]["ereignis"] == "Kano wird verletzt." and punkte[2]["unklar"] is True
    assert protokoll[2]["grund"] == "Beleg in der Abschrift nicht gefunden."
    assert a.letzte_unklar == []  # Ereignis 3 war schon als unklar bekannt (aus den Notizen gezählt)


def test_zweiter_blick_macht_unklar_und_meldet():
    class Unklar(Klient):
        def chat(self, system, nutzer):
            return sm.Antwort(json.dumps({"ereignisse": [{"nr": 1, "urteil": "unklar", "grund": "Zwei Lesarten."},
                                                          {"nr": 2, "urteil": "unsinn"}]}), 1, 1)

    a = sm.Ablauf(Unklar())
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [], "transkript": [
        {"start": t, "sprecher": z.split("] ")[1].split(":")[0], "text": z.split(": ", 1)[1]} for t, z in ZEILEN]}
    punkte = _punkte()[:2]
    protokoll = a.zweiter_blick(ein, punkte)
    assert [p["urteil"] for p in protokoll] == ["unklar", "unklar"]
    assert punkte[0]["unklar"] is True and punkte[0]["ausgang"] == "(unklar: Zwei Lesarten.)"
    assert punkte[1]["ausgang"] == "Hanna ist gerettet. (unklar: wer)"
    assert [u["quelle"] for u in a.letzte_unklar] == ["zweiter_blick", "zweiter_blick"]
    assert a.letzte_unklar[0]["notiz"] == "Die Gruppe bricht auf. – Zwei Lesarten."


class Anbieter:
    def __init__(self):
        self.aufrufe = []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{zeiten[0]}] Mara rettet Hanna aus dem Fluss."], "stand": ["Hanna: gerettet"]}
        if system.startswith("Du bereitest das Kapitel"):
            return {"ereignisse": [{"zeit": "1:00", "ereignis": "Mara rettet Hanna aus dem Fluss.",
                                    "ausgang": "Hanna lebt.", "rang": "kritisch"}]}
        if system.startswith("Du prüfst einzelne Ereignisse"):
            return {"ereignisse": [{"nr": 1, "urteil": "berichtigt", "ereignis": "Hanna rettet Mara aus dem Fluss.",
                                    "ausgang": "Mara lebt.", "zitat": "Zeile 1: Die Gruppe zieht weiter"}]}
        if system.startswith("Du schreibst den Recap"):
            return {"title": "Kapitel 1", "text": "Hanna zog Mara aus dem Fluss.", "openThreads": []}
        if system.startswith("Du vergleichst das Kapitel"):
            return {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "Hanna zog Mara aus dem Fluss"}]}
        if "Du prüfst den Recap" in system:
            return {"absaetze": [{"nr": 1, "urteil": "belegt"}]}
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


def test_im_ablauf_vor_dem_schreiben(client, world, dbs, tmp_path):
    recap_ein, vorschlag_ein = _eingabe(client, world, dbs, tmp_path)
    anbieter = Anbieter()
    a = _ablauf(anbieter)
    a.ausfuehren(recap_ein, vorschlag_ein, gegenpruefen=True)
    systeme = [x["system"][:30] for x in anbieter.aufrufe]
    blick, recap = systeme.index("Du prüfst einzelne Ereignisse "), systeme.index("Du schreibst den Recap („Was b")
    assert systeme.index("Du bereitest das Kapitel zu ei") < blick < recap
    recap_aufruf = next(x for x in anbieter.aufrufe if x["system"].startswith("Du schreibst den Recap"))
    assert "Hanna rettet Mara aus dem Fluss. – Ausgang: Mara lebt." in recap_aufruf["nutzer"]
    assert "an der Abschrift geprüft" in recap_aufruf["nutzer"]
    assert a.letzter_zweiter_blick[0]["urteil"] == "berichtigt" and a.letzte_auswahl[0]["berichtigt"] is True
    # Schalter aus: kein Aufruf
    anbieter2 = Anbieter()
    _ablauf(anbieter2, zweiter_blick_an=False).ausfuehren(recap_ein, vorschlag_ein)
    assert not any(x["system"].startswith("Du prüfst einzelne Ereignisse") for x in anbieter2.aufrufe)


def test_schalter_in_der_verwaltung(client, dbs, admin):  # noqa: F811
    from app.zusammenfassung import zweiter_blick_an

    assert zweiter_blick_an(dbs) is True
    seite = client.get("/verwaltung/zusammenfassung").text
    assert 'name="zweiter_blick" value="1" checked' in seite
    daten = {"csrf": admin, "art": "api", "anbieter": "mistral", "api_modell": "mistral-medium-latest",
             "lokal_modell": "auto", "lokal_kontext": "12288", "api_key": "sk-abcdefghijklmnop1234",
             "gegenpruefen_feld": "1", "gegenpruefen": "1"}
    client.post("/verwaltung/zusammenfassung", data=daten)  # Haken fehlt → aus
    dbs.expire_all()
    assert zweiter_blick_an(dbs) is False
    client.post("/verwaltung/zusammenfassung", data={**daten, "zweiter_blick": "1"})
    dbs.expire_all()
    assert zweiter_blick_an(dbs) is True


def test_probelauf_dateien_0474():
    from app import kapitelprobe

    assert "zweiter-blick.json" in kapitelprobe.DATEIEN
