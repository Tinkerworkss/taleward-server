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
    assert _json(Antwort('{"notizen": ["a", "b'), retten=True) == {"notizen": ["a"]}


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
