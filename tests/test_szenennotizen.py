"""Lange Runden: Szenennotizen auch dann, wenn ein kleines Modell abgeschnittenes oder kaputtes JSON liefert."""
import json

import pytest


def test_abgeschnittenes_json_wird_gerettet():
    from app.sprachmodell import json_retten

    assert json_retten('{"notizen": ["[0:01] Ankunft in Rabenfels", "[0:05] Der Wirt sagt \\"Willkommen\\"", "[0:0') \
        == {"notizen": ["[0:01] Ankunft in Rabenfels", '[0:05] Der Wirt sagt "Willkommen"']}
    assert json_retten('Hier: {"notizen": ["a", "b"], "x": [1, {"y": "z') == {"notizen": ["a", "b"]}
    assert json_retten('{"notizen": ["[0:01] nur ein halber') is None
    assert json_retten("kein json") is None


def test_recap_wird_nicht_gerettet():
    from app.sprachmodell import Antwort, AntwortFehler, _json

    with pytest.raises(AntwortFehler):
        _json(Antwort('{"title": "Kapitel 1", "text": "Die Gruppe ritt nach'))
    assert _json(Antwort('{"notizen": ["a", "b'), retten=True) == {"notizen": ["a"], "_gerettet": True}


def _ein(n=300):
    zeilen = [{"start": i * 20.0, "sprecher": "Mira" if i % 2 else "Anna", "member_id": None,
               "text": f"Satz {i} über Rabenfels und den langen Weg dorthin, es regnet seit Tagen."} for i in range(n)]
    return {"sprache": "de", "kampagne": "K", "system": None, "system_name": None, "welt": None,
            "session_nummer": 1, "session_titel": None, "gaeste": [], "personen": [], "transkript": zeilen,
            "bibel": [], "geheim": []}


def test_kaputter_abschnitt_wird_geteilt():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.laengen = []

        def chat(self, system, nutzer):
            zeilen = nutzer.count("\n[")
            self.laengen.append(zeilen)
            if zeilen > 40:  # langer Abschnitt: das Modell verliert sich
                return Antwort("Ich fasse zusammen: Die Gruppe", 10, 5)
            return Antwort(json.dumps({"notizen": [f"[0:00] Teil mit {zeilen} Zeilen"] * 3}), 10, 5)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=1500)
    titel, text = ablauf.grundlage(_ein(), lambda _p: None)
    assert titel.startswith("Szenennotizen")
    assert max(k.laengen) > 40 and min(k.laengen) <= 40  # erst lang (zwei Versuche), dann geteilt
    assert "Teil mit" in text and text.count("\n") < len(k.laengen) * 3  # gleiche Zeilen hintereinander nur einmal


def test_abschnitte_fuer_notizen_bleiben_klein():
    from app import sprachmodell
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"
        groessen: list[int] = []

        def chat(self, system, nutzer):
            self.groessen.append(sprachmodell.tokens(nutzer))
            return Antwort(json.dumps({"notizen": ["[0:00] x"]}), 10, 5)

    k = Klient()
    Ablauf(k, max_transkript_tokens=200, stueck_tokens=30000).grundlage(_ein(2000), lambda _p: None)
    assert max(k.groessen) <= sprachmodell.NOTIZ_STUECK + 500


def test_recap_laenge_nach_rundenlaenge():
    from app.sprachmodell import woerter

    assert woerter({"transkript": [{"start": 45 * 60.0}]}) == "250–600"
    assert woerter({"transkript": [{"start": 95 * 60.0}]}) == "400–900"
    assert woerter({"transkript": [{"start": 158 * 60.0}]}) == "600–1200"
    assert woerter({"transkript": []}) == "250–600"


def test_recap_aufruf_nennt_laenge_und_wendepunkte():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"
        systeme: list[str] = []

        def chat(self, system, nutzer):
            self.systeme.append(system)
            return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": []}), 1, 1)

    k = Klient()
    Ablauf(k).recap(_ein(500), "Transkript", "[0:00] Anna: los")  # 500 Zeilen × 20 s ≈ 166 min
    assert "600–1200 Wörter" in k.systeme[-1] and "Wendepunkte" in k.systeme[-1]


def test_notizen_bekommen_die_vorgeschichte():
    from app.sprachmodell import BISHER, Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = []

        def chat(self, system, nutzer):
            self.nutzer.append(nutzer)
            letzte = nutzer.rstrip().rsplit("\n", 1)[-1].split("]")[0].strip("[")  # Zeit der letzten Zeile
            return Antwort(json.dumps({"notizen": [f"[{letzte}] Notiz {len(self.nutzer)}-{j}"
                                                   for j in range(10)]}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=200, stueck_tokens=1500)
    ablauf.grundlage(_ein(300), lambda _p: None)
    assert "Bisher" not in k.nutzer[0]
    assert "Bisher (nur zur Orientierung" in k.nutzer[1] and "Notiz 1-9" in k.nutzer[1]
    assert k.nutzer[1].count("Notiz 1-") == BISHER


def test_beleg_zeit_aus_der_notiz():
    from app.sprachmodell import pruefen

    v = pruefen([{"entryType": "npc", "action": "create", "title": "Pipo", "detail": "Eine Wache.",
                  "evidence": [{"start": "0:00", "quote": "[1:09:07] Pipo ist schwer verletzt"},
                               {"quote": "[12:34] Pipo gibt sein Schwert"},
                               {"start": "5:00", "quote": "[12:34] bleibt"}]}], set(), set())
    assert [b["start"] for b in v[0]["evidence"]] == [4147.0, 754.0, 300.0]


def test_lange_runde_recap_aus_teilen():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            if "4 bis 8 Sätzen" in system:
                nr = nutzer.split("Teil ")[1].split(" ")[0]
                return Antwort(json.dumps({"zusammenfassung": f"Im Teil {nr} geschieht etwas."}), 1, 1)
            if "Szenennotizen" in system:
                return Antwort(json.dumps({"notizen": [f"[{len(self.aufrufe)}:00] Ereignis {len(self.aufrufe)}-{j} "
                                                       + "mit vielen Worten " * 10 for j in range(12)]}), 1, 1)
            if "Recap" in system and "prüfst" not in system:
                return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": []}), 1, 1)
            return Antwort(json.dumps({"proposals": [], "absaetze": []}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=2000, stueck_tokens=1500)
    ein = _ein(600)
    ablauf.ausfuehren(ein, ein)
    teile = [n for s, n in k.aufrufe if "4 bis 8 Sätzen" in s]
    assert len(teile) >= 2 and "Teil 1 von" in teile[0]
    recap = [n for s, n in k.aufrufe if "Was bisher geschah" in s][0]
    assert "Verlauf der Runde in Teilen" in recap and "Im Teil 1 geschieht etwas." in recap
    assert ablauf.letzter_verlauf.startswith("Teil 1 von")
    vorschlag = [n for s, n in k.aufrufe if "Kampagnen-Bibel" in s][0]
    assert "Ereignis" in vorschlag  # Vorschläge arbeiten weiter mit den genauen Notizen


def test_kurze_runde_ohne_teile():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"
        systeme: list[str] = []

        def chat(self, system, nutzer):
            self.systeme.append(system)
            return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": [],
                                       "proposals": []}), 1, 1)

    k = Klient()
    ein = _ein(50)
    Ablauf(k).ausfuehren(ein, ein)
    assert not any("4 bis 8 Sätzen" in s for s in k.systeme)


def test_abgeschnittene_notizen_rest_wird_nachgeholt():
    """Bricht die Antwort ab (Längengrenze), fehlt das Ende des Abschnitts – es wird eigens nachgeholt."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = []

        def chat(self, system, nutzer):
            self.nutzer.append(nutzer)
            zeilen = [z for z in nutzer.split("\n") if z.startswith("[")]
            if len(self.nutzer) == 1:  # erster Aufruf: nur die erste Hälfte, dann abgeschnitten
                halbe = zeilen[: len(zeilen) // 2]
                text = json.dumps({"notizen": [f"{z.split(' ')[0]} Notiz zu {z.split(' ')[0]}" for z in halbe]})
                return Antwort(text[:-8], 1, 1)
            return Antwort(json.dumps({"notizen": [f"{z.split(' ')[0]} Notiz zu {z.split(' ')[0]}" for z in zeilen[::3]]}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=50000)
    _, text = ablauf.grundlage(_ein(300), lambda _p: None)
    # Abschnitt 1 abgeschnitten → sein Rest wird nachgeholt; danach Abschnitt 2 normal: ein Aufruf mehr als Abschnitte
    abschnitte = k.nutzer[0].split("Abschnitt 1 von ")[1].split(" ")[0]
    assert len(k.nutzer) == int(abschnitte) + 1
    assert "[1:39:40]" in text  # das Ende der Runde (Zeile 299 × 20 s) ist in den Notizen
    erste_zeiten = [int(z.split(":")[0].strip("[")) for z in text.split("\n")[:3]]
    assert erste_zeiten == sorted(erste_zeiten)  # Reihenfolge bleibt


def test_notizen_mit_regeln_fliegen_raus():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            return Antwort(json.dumps({"notizen": [
                "[0:00] Alle verlieren 8 Lebenspunkte wegen Folter.",
                "[0:30] Spielleitung fordert eine Probe auf Klettern.",
                "[1:00] Orasilas verbindet Pipo notdürftig.",
                "[99:00] Die Gruppe erreicht den Fringlasshof."]}), 1, 1)

    _, text = Ablauf(Klient(), max_transkript_tokens=200, stueck_tokens=50000).grundlage(_ein(30), lambda _p: None)
    assert "Pipo" in text and "Fringlasshof" in text and "Lebenspunkte" not in text and "Probe" not in text
