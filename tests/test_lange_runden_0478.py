"""Server 0.4.78: lange Runden. Mehr Platz und mehr Pflichtereignisse ab 3 h, Teile im Kapitel nur an einem
natürlichen Einschnitt, Stand je Auswahl-Abschnitt nur bis zu seinem Ende, Schutz gegen einen zusammengefallenen
Entwurf, Reihenfolge als Hinweis in der Prüfansicht, doppelter Kapiteltitel."""
import json
import re

import httpx

from app import sprachmodell as sm


def test_laenge_und_pflichtereignisse_nach_dauer():
    assert [sm.woerter_fuer(m, True) for m in (50, 100, 150, 200, 260, 430)] == [
        "250–600", "400–900", "900–1500", "1200–1800", "1500–2100", "1800–2400"]
    assert [sm.woerter_fuer(m) for m in (150, 200, 260, 430)] == ["600–1200", "800–1400", "1000–1600", "1200–1800"]
    assert [sm.auswahl_hoechstens_fuer(m) for m in (50, 100, 150, 200, 260, 430)] == [12, 20, 30, 35, 40, 45]
    ein = {"transkript": [{"start": 429 * 60.0}]}
    assert sm.untergrenze(ein, True) == 1800 and sm.obergrenze(ein) == 1800 and sm.auswahl_hoechstens(ein) == 45


def test_stand_bis_laesst_spaeteres_weg():
    stand = ["Ana: hat Jobangebot (seit [2:06:56]); jetzt Auftrag (3.000 pro Kopf, bis Freitag, seit [4:54:25]).",
             "Gruppe: nimmt den Auftrag an (3.000 pro Kopf) (seit [4:54:10]).",
             "Abmachungen: Bo und Cem – Wohnung gegen Miete; Gruppe und Dov – Auftrag",
             "Bo: lebt am Hafen (seit [26:36])."]
    frueh = sm.stand_bis(stand, 2 * 3600 + 30 * 60)
    assert frueh == ["Ana: hat Jobangebot (seit [2:06:56])", stand[2], stand[3]]
    assert not any("3.000" in z for z in frueh)
    assert sm.stand_bis(stand, 5 * 3600) == [z.replace("; ", "; ") for z in stand]  # nach dem Ende: alles
    assert sm.stand_bis(["Ana: (seit [5:00:00])"], 60.0) == []


class Auswahlklient:
    modell = "m"

    def __init__(self):
        self.aufrufe = []

    def chat(self, system, nutzer):
        self.aufrufe.append((system, nutzer))
        return sm.Antwort(json.dumps({"ereignisse": []}), 1, 1)


def test_auswahl_sieht_stand_nur_bis_zum_abschnittsende():
    k = Auswahlklient()
    a = sm.Ablauf(k)
    a.letzter_stand = ["Ana: Auftrag angenommen (3.000 pro Kopf, seit [2:20:00])", "Bo: lebt am Hafen"]
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [], "transkript": [{"start": 155 * 60.0}]}
    a.auswahl(ein, "\n".join(f"[{m // 60}:{m % 60:02d}:00] Notiz {m}." for m in range(0, 155, 5)))
    assert len(k.aufrufe) == 4
    erster, letzter = k.aufrufe[0][1], k.aufrufe[3][1]
    assert "Stand bis 38:45" in erster and "3.000" not in erster and "Bo: lebt am Hafen" in erster
    assert "Stand am Ende der Runde" in letzter and "3.000" in letzter
    assert "was erst später vereinbart" in k.aufrufe[0][0]
    # ein Teil der Runde: Abschnitte ab seinem Beginn
    b = sm.auswahl_abschnitte("[3:20:00] a.\n[4:50:00] b.", 120.0, 30, start=3 * 3600.0)
    assert b[0]["von"] == 3 * 3600.0 and abs(b[-1]["bis"] - 5 * 3600.0) < 1e-6 and "b." in b[-1]["notizen"]


def test_recap_text_liest_gegliederte_antwort():
    einleitung = "Die Stadt lag im Regen. " * 10
    abschnitte = [{"titel": "Anwerbung", "text": "Ana nahm den Auftrag an und traf Bo im Hafen. " * 6},
                  {"titel": "Treffen", "text": "In der Kneipe legte Dov einen Datenchip auf den Tisch. " * 6}]
    t = sm.recap_text({"title": "Kapitel 1: X", "text": einleitung, "abschnitte": abschnitte, "openThreads": ["a" * 300]})
    teile = sm.absaetze(t)
    assert len(teile) == 3 and teile[1].startswith("Ana nahm") and teile[2].startswith("In der Kneipe")
    # eine Zusammenfassung, die nur wiederholt, kommt nicht dazu; kurze Felder und Fremdes auch nicht
    lang = "Ana nahm den Auftrag an und traf Bo im Hafen, dann legte Dov einen Datenchip auf den Tisch. " * 5
    assert sm.recap_text({"text": lang, "summary": lang.upper().lower() + " Ende.", "notes": "x " * 100}) == lang.strip()
    assert sm.recap_text({"text": "kurz", "story": "auch kurz"}) == "kurz"
    assert sm.recap_text({"recap": {"text": "verschachtelt"}}) == "verschachtelt"


def test_titel_doppelt():
    assert sm.titel_saeubern("Kapitel 1: Kapitel 1: Schatten über der Stadt") == "Kapitel 1: Schatten über der Stadt"
    assert sm.titel_saeubern("Chapter 3 – Chapter 3: Rain") == "Chapter 3: Rain"
    assert sm.titel_saeubern("Kapitel 2: Der Auftrag") == "Kapitel 2: Der Auftrag"
    from app.zusammenfassung import ergebnis_aus

    assert ergebnis_aus({"title": "Kapitel 1: Kapitel 1: Nacht", "text": "t"}).titel == "Kapitel 1: Nacht"


def test_reihenfolge_als_hinweis():
    text = "Bo rettet Ana aus dem Wasser.\n\nAna fällt ins Wasser.\n\nDer Abend endet.\n\nAna dankt Bo am Feuer."
    punkte = [{"zeit": 600.0, "ereignis": "Ana fällt ins Wasser.", "ausgang": "", "rang": "kritisch"},
              {"zeit": 3000.0, "ereignis": "Bo rettet Ana.", "ausgang": "gerettet", "rang": "kritisch"},
              {"zeit": 3100.0, "ereignis": "Ana dankt Bo.", "ausgang": "", "rang": "wichtig"}]
    d = {"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 2, "zitat": "Ana fällt ins Wasser"},
                    {"nr": 2, "status": "erzaehlt", "absatz": 1, "zitat": "Bo rettet Ana aus dem Wasser"},
                    {"nr": 3, "status": "erzaehlt", "absatz": 4, "zitat": "Ana dankt Bo am Feuer"}]}
    befund = sm.pflicht_lesen(d, 3, 4, text, "de", punkte)
    # Rettung (50 min) steht einen Absatz vor dem Sturz (10 min): zu nah für einen Hinweis
    assert all("reihenfolge" not in b for b in befund)
    text2 = "Bo rettet Ana aus dem Wasser.\n\nDer Abend beginnt.\n\nAna fällt ins Wasser.\n\nAna dankt Bo am Feuer."
    d["punkte"][0]["absatz"] = 3
    befund = sm.pflicht_lesen(d, 3, 4, text2, "de", punkte)
    assert befund[1].get("reihenfolge") == 1 and "reihenfolge" not in befund[0] and befund[1]["status"] == "erzaehlt"
    p = sm.pruefansicht_aus_pflicht(text2, punkte, befund, [], [], "m", False)
    a = p["paragraphs"][0]
    assert a["verdict"] == "partial" and a["note"].startswith("Reihenfolge prüfen: „Bo rettet Ana.“ (50:00)")
    assert "Ana fällt ins Wasser." in a["note"] and p["paragraphs"][2]["verdict"] == "supported"


NOTIZEN = "\n".join(f"[{m // 60}:{m % 60:02d}:00] Szene bei Minute {m}." for m in range(0, 430, 10)).replace(
    "[3:10:00] Szene bei Minute 190.", "[3:10:00] Am nächsten Abend treffen sich alle in der Kneipe.")
DAUER = 430 * 60.0


def test_einschnitte_lesen_prueft_lage_zitat_und_dauer():
    gut = {"titel_anfang": "Die Anwerbung", "einschnitte": [
        {"zeit": "3:10:00", "art": "zeitsprung", "zitat": "Am nächsten Abend treffen sich alle", "titel": "Der Auftrag"}]}
    teile = sm.einschnitte_lesen(gut, NOTIZEN, DAUER, 2)
    assert teile == [{"von": 0.0, "bis": 11400.0, "titel": "Die Anwerbung"},
                     {"von": 11400.0, "bis": DAUER, "titel": "Der Auftrag"}]
    # Zitat nicht in den Notizen, falsche Art, außerhalb 30–70 %, kein Einschnitt: keine Teile
    assert sm.einschnitte_lesen({"einschnitte": [dict(gut["einschnitte"][0], zitat="erfunden ganz und gar")]},
                                NOTIZEN, DAUER, 2) == []
    assert sm.einschnitte_lesen({"einschnitte": [dict(gut["einschnitte"][0], art="gefuehl")]}, NOTIZEN, DAUER, 2) == []
    frueh = {"einschnitte": [{"zeit": "1:00:00", "art": "ortswechsel", "zitat": "Szene bei Minute 60", "titel": "x"}]}
    assert sm.einschnitte_lesen(frueh, NOTIZEN, DAUER, 2) == []
    assert sm.einschnitte_lesen({"einschnitte": []}, NOTIZEN, DAUER, 2) == []
    # zwei Einschnitte nur mit 60 Minuten Abstand; Ersatztitel ohne Überschrift (0.4.79: zwei nur als Zeitsprünge)
    zwei = {"einschnitte": [gut["einschnitte"][0],
                            {"zeit": "3:40:00", "art": "zeitsprung", "zitat": "Szene bei Minute 220", "titel": ""},
                            {"zeit": "4:50:00", "art": "zeitsprung", "zitat": "Szene bei Minute 290", "titel": "Teil 3: Flucht"}]}
    teile = sm.einschnitte_lesen(zwei, NOTIZEN, DAUER, 2)
    assert [t["von"] for t in teile] == [0.0, 11400.0, 17400.0] and [t["titel"] for t in teile] == ["Teil 1", "Der Auftrag", "Flucht"]


def test_teil_notizen_und_verorten():
    n = sm.teil_notizen("[0:10:00] a.\nweiter a\n[3:10:00] b.\n[5:00:00] c.", 3 * 3600.0, 4 * 3600.0)
    assert n == "[3:10:00] b."
    text = "Ein Anfang im Regen.\n\nNoch mehr.\n\nAm nächsten Abend trafen sich alle in der Kneipe.\n\nSchluss."
    assert sm.teile_verorten(text, ["Ein Anfang im Regen.", "Am nächsten Abend trafen sich alle in der Kneipe."],
                             ["A", "B"]) == [{"title": "A", "firstParagraph": 0}, {"title": "B", "firstParagraph": 2}]
    # umformuliert: über gemeinsame Wortstämme; nicht wiederzufinden: keine Teile
    umgestellt = text.replace("Am nächsten Abend trafen", "Einen Abend später trafen")
    assert sm.teile_verorten(umgestellt, ["x", "Am nächsten Abend trafen sich alle in der Kneipe."], ["A", "B"])[1][
        "firstParagraph"] == 2
    assert sm.teile_verorten(text, ["x", "Völlig anderer Text über Drachen."], ["A", "B"]) == []


_NAMEN = ["Ana", "Bo", "Cem", "Dov", "Eli", "Fay", "Gil", "Hal", "Ira", "Jon"]
_TUN = ["betrat", "verließ", "suchte", "bewachte", "öffnete", "verkaufte", "reparierte", "beschrieb", "mied", "kaufte"]
_ORTE = ["Hafen", "Kneipe", "Lagerhalle", "Brücke", "Werkstatt", "Garage", "Markthalle", "Kirche", "Tankstelle",
         "Bahnhof"]
_ZEIT = ["früh", "mittags", "abends", "nachts", "später", "danach", "zuletzt", "eilig", "leise", "heimlich", "offen",
         "zögernd"]


def _satz(k, j, i):
    return f"{_NAMEN[i]} {_TUN[(i + j) % 10]} {_ZEIT[j]} die {_ORTE[(i + 3 * j + int(k)) % 10]} im {['ganzen', 'ersten', 'zweiten'][int(k)]} Teil der langen Nacht."


class Anbieter:
    """Ein Anbieter, der je Aufgabe antwortet; der erste Recap fällt zusammen (nur die Einleitung)."""

    def __init__(self, zusammenfallen=True, einschnitt=True):
        self.aufrufe, self.zusammenfallen, self.einschnitt = [], zusammenfallen, einschnitt
        self.recaps = []

    def antwort(self, system: str, nutzer: str) -> dict:
        if "Schreibe Szenennotizen" in system:
            eigen = nutzer.split("des Transkripts:\n")[1]
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", eigen, re.M)
            return {"notizen": [f"[{z}] " + ("Am nächsten Abend treffen sich alle." if z.startswith("3:10")
                                             else f"Szene um {z} mit Ana.") for z in zeiten], "stand": []}
        if system.startswith("Du gliederst"):
            return {"titel_anfang": "Die Anwerbung", "einschnitte": [
                {"zeit": "3:10:00", "art": "zeitsprung", "zitat": "Am nächsten Abend treffen sich alle",
                 "titel": "Der Auftrag"}] if self.einschnitt else []}
        if system.startswith("Du bereitest das Kapitel"):
            zeiten = re.findall(r"^\[(\d+:\d{2}(?::\d{2})?)\]", nutzer, re.M)[:2]
            return {"ereignisse": [{"zeit": z, "ereignis": f"Ana handelt um {z}.", "ausgang": "", "rang": "kritisch"}
                                   for z in zeiten]}
        if system.startswith("Du schreibst den Recap"):
            self.recaps.append((system, nutzer))
            teil = re.search(r"Teil (\d) von", nutzer)
            k = teil.group(1) if teil else "0"
            if self.zusammenfallen and len(self.recaps) == 1:
                return {"title": "Kapitel 1: Kapitel 1: Regen", "text": "Die Stadt lag im Regen.", "openThreads": []}
            return {"title": f"Kapitel 1: Teil {k}", "text": "\n\n".join(
                " ".join(_satz(k, j, i) for i in range(10)) for j in range(12)), "openThreads": [f"Faden {k}"]}
        if system.startswith("Du vergleichst das Kapitel"):
            return {"punkte": []}
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


def _eingabe_lang():
    zeilen = [{"start": float(m * 60), "sprecher": "Ana" if m % 20 else "Bo", "text": f"Satz bei Minute {m}."}
              for m in range(0, 430, 10)]
    return {"sprache": "de", "kampagne": "K", "session_nummer": 1, "session_titel": None, "welt": "", "bibel": [],
            "personen": [], "transkript": zeilen}


def _ablauf(anbieter):
    klient = sm.OpenAIKlient("https://llm.example/v1", "sk", "m", httpx.Client(transport=httpx.MockTransport(anbieter)))
    return sm.Ablauf(klient, schritt=lambda _n: None, nachbesserung=False, notizen_zuerst=True, notiz_stueck=200000,
                     zweiter_blick_an=False)


def test_lange_runde_in_teilen():
    an = Anbieter()
    a = _ablauf(an)
    d = a.ausfuehren(_eingabe_lang(), dict(_eingabe_lang(), geheim=[]), gegenpruefen=True)
    assert d["title"] == "Kapitel 1: Teil 1"
    assert d["parts"][0] == {"title": "Die Anwerbung", "firstParagraph": 0}
    assert d["parts"][1]["title"] == "Der Auftrag"
    teile = sm.absaetze(d["text"])
    i = d["parts"][1]["firstParagraph"]
    assert "zweiten Teil" in teile[i] and "ersten Teil" in teile[i - 1]
    assert d["openThreads"] == ["Faden 2"]
    # Teil 1 kam zusammengefallen zurück und wurde einmal neu angefordert; die Rohantwort bleibt für die Fehlersuche
    assert len(an.recaps) == 3 and "Dein erster Entwurf hatte nur 5 Wörter" in an.recaps[1][1]
    assert a.letzter_kurzer_entwurf[0]["woerter"] == 5 and "Die Stadt lag im Regen" in a.letzter_kurzer_entwurf[0]["antwort"]
    # Teil 1 sieht den Stand nur bis zum Einschnitt, Teil 2 setzt fort und knüpft an
    assert "Teil 1 von 2 eines Kapitels" in an.recaps[0][0] and "Kein Schlusswort" in an.recaps[0][0]
    assert "setzt den Teil davor nahtlos fort" in an.recaps[2][0] and "So endet der Teil davor" in an.recaps[2][1]
    assert "Szenennotizen von Teil 2 von 2 (3:10:00–7:00:00)" in an.recaps[2][1]
    assert "[3:10:00]" not in an.recaps[0][1] and "[3:10:00]" in an.recaps[2][1]
    # 0.4.79: die Teile teilen sich den Platz der ganzen Runde (7 h: 1800–2400) nach ihrer Dauer
    assert a.grenzen(_eingabe_lang()) == (1800, 2400)
    assert "1090 Wörtern" in an.recaps[0][0] and "1310 Wörtern" in an.recaps[2][0]


def test_ohne_einschnitt_ein_kapitel():
    an = Anbieter(zusammenfallen=False, einschnitt=False)
    a = _ablauf(an)
    d = a.ausfuehren(_eingabe_lang(), dict(_eingabe_lang(), geheim=[]), gegenpruefen=True)
    assert "parts" not in d and a.letzte_teile == [] and len(an.recaps) == 1
    assert "1800 bis 2400 Wörtern" in an.recaps[0][0]  # über 5 h: mehr Platz, mit Deckel
    assert a.letzter_kurzer_entwurf == []


def test_nachbesserung_teilt_langen_absatz():
    class K:
        modell = "m"

        def chat(self, system, nutzer):
            satz = "Ana nahm den Auftrag an und ging danach mit Bo zum Hafen hinunter. "
            return sm.Antwort(json.dumps({"absaetze": [{"nr": 1, "text": satz * 40}]}), 1, 1)

    a = sm.Ablauf(K())
    punkte = [{"zeit": 1.0, "ereignis": f"E{i}", "ausgang": "", "rang": "kritisch"} for i in range(3)]
    befund = [{"nr": i + 1, "status": "fehlt", "absatz": 0} for i in range(3)]
    neu = a.pflicht_nachbessern({"kampagne": "K", "session_nummer": 1, "personen": []}, punkte, befund, "Ana kam an.\n\nEnde.")
    assert len(sm.absaetze(neu)) >= 4 and all(len(x.split()) <= sm.ABSATZ_HOECHSTENS for x in sm.absaetze(neu))


def test_hinweise_filtern():
    p = {"paragraphs": [{"index": 0, "note": "Nicht erzählt: Ana schlägt den WÄCHTER (Probe 3 Erfolge)."},
                        {"index": 1, "note": None}]}
    sm.hinweise_filtern(p)
    assert p["paragraphs"][0]["note"] == "Nicht erzählt: Ana schlägt den Wächter." and p["paragraphs"][1]["note"] is None
