"""Lange Runden: Szenennotizen auch dann, wenn ein kleines Modell abgeschnittenes oder kaputtes JSON liefert."""
import json
import re

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
            return Antwort(json.dumps({"notizen": ["[0:00] Die Gruppe bricht auf."]}), 10, 5)

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
            return Antwort(json.dumps({"notizen": [f"[{letzte}] Notiz {len(self.nutzer)}-{j} der Runde"
                                                   for j in range(10)]}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=200, stueck_tokens=1500)
    ablauf.grundlage(_ein(300), lambda _p: None)
    assert "Bisher" not in k.nutzer[0]
    assert "Bisher (nur zur Orientierung" in k.nutzer[1] and "Notiz 1-9 der Runde" in k.nutzer[1]
    assert k.nutzer[1].count("Notiz 1-") == BISHER


def test_beleg_zeit_aus_der_notiz():
    from app.sprachmodell import pruefen

    v = pruefen([{"entryType": "npc", "action": "create", "title": "Pipo", "detail": "Eine Wache.",
                  "evidence": [{"start": "0:00", "quote": "[1:09:07] Pipo ist schwer verletzt"},
                               {"quote": "[12:34] Pipo gibt sein Schwert"},
                               {"start": "5:00", "quote": "[12:34] bleibt"}]}], set(), set())
    assert [b["start"] for b in v[0]["evidence"]] == [4147.0, 754.0, 300.0]


def test_lange_runde_recap_aus_teilen():
    """Ausweichlösung: Die Notizen bleiben auch nach dem Verdichten zu groß → Teil-Zusammenfassungen."""
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
            if "Schreibe Szenennotizen" in system:
                return Antwort(json.dumps({"notizen": [f"[{len(self.aufrufe)}:00] Ereignis {len(self.aufrufe)}-{j} "
                                                       + "mit vielen Worten " * 10 for j in range(12)]}), 1, 1)
            if system.startswith("Du klassifizierst die Szenennotizen"):
                ids = [z.split(" |", 1)[0] for z in nutzer.splitlines() if z.startswith("N") and " |" in z]
                return Antwort(json.dumps({"critical": [], "important": ids, "minor": []}), 1, 1)
            if system.startswith("Du vergleichst den Recap"):
                return Antwort(json.dumps({"fehlend": []}), 1, 1)
            if system.startswith("Du pflegst die Kampagnen-Bibel"):
                return Antwort(json.dumps({"proposals": []}), 1, 1)
            if "Was bisher geschah" in system:
                return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": []}), 1, 1)
            raise AssertionError("unerwarteter Prompt im Test")

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
            if system.startswith("Du vergleichst den Recap"):
                return Antwort(json.dumps({"fehlend": []}), 1, 1)
            if system.startswith("Du pflegst die Kampagnen-Bibel"):
                return Antwort(json.dumps({"proposals": []}), 1, 1)
            if "Was bisher geschah" in system:
                return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": []}), 1, 1)
            raise AssertionError("unerwarteter Prompt im Test")

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
                text = json.dumps({"notizen": [f"{z.split(' ')[0]} Notiz zur Zeile bei {z.split(' ')[0]}" for z in halbe]})
                return Antwort(text[:-8], 1, 1)
            return Antwort(json.dumps({"notizen": [f"{z.split(' ')[0]} Notiz zur Zeile bei {z.split(' ')[0]}" for z in zeilen[::3]]}), 1, 1)

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


def test_notizen_ohne_zeit_werden_neu_angefordert():
    """Lauf 7: Ein Abschnitt kam ohne Zeitstempel zurück, der Rest wurde nachgeholt – alles stand doppelt da.
    Jetzt: einmal mit Erinnerung neu anfordern, den zweiten Versuch nehmen."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = []

        def chat(self, system, nutzer):
            self.nutzer.append(nutzer)
            zeilen = [z for z in nutzer.split("\n") if z.startswith("[")]
            if "Notizen ohne Zeitstempel sind unbrauchbar" not in nutzer:
                return Antwort(json.dumps({"notizen": ["Der Galgen bricht auseinander.", "Alle fallen ins Wasser."]}), 1, 1)
            return Antwort(json.dumps({"notizen": [f"{z.split(' ')[0]} Notiz zur Zeile bei {z.split(' ')[0]}"
                                                   for z in zeilen[::4]]}), 1, 1)

    k = Klient()
    _, text = Ablauf(k, max_transkript_tokens=200, stueck_tokens=50000).grundlage(_ein(40), lambda _p: None)
    assert len(k.nutzer) == 2
    assert "Galgen" not in text and text.startswith("[0:00]") and "[12:00]" in text


def test_notizen_normiert_bruchstuecke_und_doppelte():
    from app.sprachmodell import Ablauf, Antwort, _ohne_doppelte, _notiz_normieren

    assert _notiz_normieren("19:31] Eine Menge Schritte.") == "[19:31] Eine Menge Schritte."
    assert _notiz_normieren("[1:02:03]Text") == "[1:02:03] Text"
    assert _ohne_doppelte(["[0:01] Pipo gibt sein Schwert.", "[0:05] Pipo gibt sein Schwert!", "[0:09] Weiter geht es."]) \
        == ["[0:01] Pipo gibt sein Schwert.", "[0:09] Weiter geht es."]

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            return Antwort(json.dumps({"notizen": [
                "[0:00] Orasilas verbindet Pipo notdürftig.",
                "[2:22:14] Die",
                "9:00] Pipo gibt der Gruppe sein Schwert.",
                "[9:30] Alle müssen eine Schwimmprobe machen.",
                "[9:40] Orasilas kritisch erfolgreich bei Raufen.",
                "[9:50] Spielleitung kündigt eine Heldenprüfung an.",
                "[10:00] Die Gruppe erreicht den Fringlasshof."]}), 1, 1)

    _, text = Ablauf(Klient(), max_transkript_tokens=200, stueck_tokens=50000).grundlage(_ein(30), lambda _p: None)
    assert text.split("\n") == ["[0:00] Orasilas verbindet Pipo notdürftig.", "[9:00] Pipo gibt der Gruppe sein Schwert.",
                                "[10:00] Die Gruppe erreicht den Fringlasshof."]


def test_beleg_zeit_aus_zitierter_notiz():
    from app.sprachmodell import pruefen

    notizen = "[24:48] Sie laufen über die Kaspomirbrücke über den Tommelfluss zum Kasmiringenplatz.\n[2:34:01] Holzbein."
    roh = [{"entryType": "location", "action": "create", "title": "Kasmiringenplatz", "detail": "Ein Platz.",
            "evidence": [{"start": 0.0, "quote": "Sie laufen über die Kaspomirbrücke über den Tommelfluss zum Kasmiringenplatz."},
                         {"start": 0.0, "quote": "Ganz etwas anderes, das in keiner Notiz steht."}]}]
    belege = pruefen(roh, set(), set(), grundlage=notizen)[0]["evidence"]
    assert belege[0]["start"] == 24 * 60 + 48 and belege[1]["start"] == 0.0


def test_notizen_erben_zeit_und_abschriften_fallen_weg():
    """Lauf 8: Ein Abschnitt bestand aus abgeschriebenen Transkriptzeilen, je gefolgt von einer Notiz ohne Zeit."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            zeilen = [z for z in nutzer.split("\n") if z.startswith("[")]
            return Antwort(json.dumps({"notizen": [
                zeilen[2],  # wörtlich abgeschrieben, mit Sprecher
                "Die Gruppe spricht über Rabenfels und den Regen.",  # ohne Zeit → erbt die Zeit der Notiz davor
                "[1:00] Spielleitung: " + zeilen[3].split(": ", 1)[1],  # abgeschrieben, anderer Sprecher
                "[2:00] Mira bricht nach Rabenfels auf.",
                "Anna bleibt zurück und wartet.",
                "[9:00] Alle erreichen Rabenfels im Regen."]}), 1, 1)

    _, text = Ablauf(Klient(), max_transkript_tokens=200, stueck_tokens=50000).grundlage(_ein(30), lambda _p: None)
    assert text.split("\n") == ["[0:40] Die Gruppe spricht über Rabenfels und den Regen.",
                                "[2:00] Mira bricht nach Rabenfels auf.", "[2:00] Anna bleibt zurück und wartet.",
                                "[9:00] Alle erreichen Rabenfels im Regen."]


def test_teile_nach_spielzeit():
    from app.sprachmodell import _teile_nach_zeit, TEIL_MINUTEN

    # 90 Minuten, aber die ersten 10 Minuten sind wortreich: trotzdem drei Teile nach Zeit
    notizen = [f"[{m}:00] " + ("Lange Notiz über den Kerker, die viel Platz braucht. " * 6) for m in range(0, 10)]
    notizen += [f"[{m}:00] Kurze Notiz." for m in range(10, 91, 5)]
    teile = _teile_nach_zeit(notizen)
    assert len(teile) == 3
    assert teile[0].startswith("[0:00]") and "[30:00]" in teile[1] and teile[2].endswith("[90:00] Kurze Notiz.")
    # ohne Zeiten: nach Textmenge wie bisher
    assert _teile_nach_zeit(["a", "b"]) == ["a\nb"]
    assert TEIL_MINUTEN == 30


def test_plan_teile_sind_feste_15_minuten_fenster():
    from app.sprachmodell import _plan_teile_nach_zeit

    notizen = [
        "[0:00] Anfang.",
        "[14:59] Noch im ersten Fenster.",
        "[15:00] Zweites Fenster.",
        "[29:59] Noch im zweiten Fenster.",
        "[30:00] Drittes Fenster.",
    ]
    teile = _plan_teile_nach_zeit(notizen)
    assert len(teile) == 3
    assert "[14:59]" in teile[0] and "[15:00]" not in teile[0]
    assert "[15:00]" in teile[1] and "[29:59]" in teile[1]
    assert teile[2].startswith("[30:00]")


def test_spielleitung_im_recap_wird_beanstandet():
    from app.sprachmodell import spielleitung_beanstanden, BEANSTANDET

    text = "Die Gruppe floh.\n\nDie Spielleitung sicherte zu, Arbon zu schützen.\n\nAm Ende schliefen alle."
    befund = [{"index": i, "verdict": "supported", "note": None, "evidence": []} for i in range(3)]
    aus = spielleitung_beanstanden(befund, text)
    assert [b["verdict"] for b in aus] == ["supported", "off_game", "supported"]
    assert aus[1]["verdict"] in BEANSTANDET and "Nichtspielercharakter" in aus[1]["note"]


def test_spielleitung_ohne_namen_im_transkript():
    from app.sprachmodell import _kopf, SYSTEM_NOTIZEN, SYSTEM_RECAP

    ein = _ein(3)
    ein["personen"] = [{"id": "1", "name": "Tinker", "rolle": "gm", "charakter": None},
                       {"id": "2", "name": "Ben", "rolle": "player", "charakter": "Mira"}]
    kopf = _kopf(ein)
    assert "Tinker" not in kopf and "Spielleitung" in kopf and "Ben spielt Mira" in kopf
    assert "„Spielleitung“ kommt" in SYSTEM_NOTIZEN and "„Spielleitung“ kommt im Recap nicht vor" in SYSTEM_RECAP


def _klient_lange_runde():
    from app.sprachmodell import Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            if "4 bis 8 Sätzen" in system:
                nr = nutzer.split("Teil ")[1].split(" ")[0]
                return Antwort(json.dumps({"zusammenfassung": f"Im Teil {nr} geschieht etwas."}), 1, 1)
            if "Schreibe Szenennotizen" in system:
                zeilen = [z for z in nutzer.split("\n") if z.startswith("[")]
                return Antwort(json.dumps({"notizen": [f"{z.split(' ')[0]} Ereignis bei {z.split(' ')[0]} geschieht."
                                                       for z in zeilen[::6]]}), 1, 1)
            if system.startswith("Du klassifizierst die Szenennotizen"):
                ids = [z.split(" |", 1)[0] for z in nutzer.splitlines() if z.startswith("N") and " |" in z]
                return Antwort(json.dumps({"critical": [], "important": ids, "minor": []}), 1, 1)
            if system.startswith("Du vergleichst den Recap"):
                return Antwort(json.dumps({"fehlend": []}), 1, 1)
            if system.startswith("Du prüfst genau EINEN Absatz"):
                return Antwort(json.dumps({"claims": []}), 1, 1)
            if system.startswith("Du prüfst den Recap"):
                return Antwort(json.dumps({"absaetze": []}), 1, 1)
            if system.startswith("Du ergänzt den Recap") or system.startswith("Du überarbeitest einzelne Absätze"):
                return Antwort(json.dumps({"absaetze": []}), 1, 1)
            if system.startswith("Du pflegst die Kampagnen-Bibel"):
                return Antwort(json.dumps({"proposals": []}), 1, 1)
            if "Was bisher geschah" in system:
                return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt.", "openThreads": []}), 1, 1)
            raise AssertionError("unerwarteter Prompt im Test")

    return Klient()


def test_lange_runde_notizen_direkt_in_zeitabschnitten():
    """Phase 1: Passen die Notizen in den Kontext, bekommt der Recap sie direkt, in Zeitabschnitte gegliedert –
    ohne die zweite Verdichtung, die Fakten kostet."""
    from app.sprachmodell import Ablauf

    k = _klient_lange_runde()
    ablauf = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=1500)
    ein = _ein(600)  # 200 min
    ablauf.ausfuehren(ein, ein)
    assert not any("4 bis 8 Sätzen" in s for s, _ in k.aufrufe)
    recap = [n for s, n in k.aufrufe if "Was bisher geschah" in s][0]
    assert "Szenennotizen der Runde in Zeitabschnitten" in recap
    assert "Abschnitt 1 von 7 (" in recap and "Abschnitt 7 von 7 (" in recap  # 200 min ≈ 7 × 30 min
    assert "Ereignis bei [0:00]" in recap and ablauf.letzter_verlauf == ""


def test_lange_runde_teile_erzwingen():
    from app.sprachmodell import Ablauf

    k = _klient_lange_runde()
    ablauf = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=1500, gliederung="teile")
    ein = _ein(600)
    ablauf.ausfuehren(ein, ein)
    assert sum(1 for s, _ in k.aufrufe if "4 bis 8 Sätzen" in s) == 7
    recap = [n for s, n in k.aufrufe if "Was bisher geschah" in s][0]
    assert "Verlauf der Runde in Teilen" in recap and ablauf.letzter_verlauf.startswith("Teil 1 von 7")


def test_temperatur_nur_fuer_notizen():
    from app.sprachmodell import Ablauf

    k = _klient_lange_runde()
    k.temperatur = None
    gesehen = []
    chat = k.chat

    def merken(system, nutzer):
        gesehen.append((("Schreibe Szenennotizen" in system), k.temperatur))
        return chat(system, nutzer)

    k.chat = merken
    ein = _ein(120)
    Ablauf(k, max_transkript_tokens=300, stueck_tokens=1500, temperatur_notizen=0.1).ausfuehren(ein, ein)
    assert all(t == 0.1 for notiz, t in gesehen if notiz) and all(t is None for notiz, t in gesehen if not notiz)
    assert k.temperatur is None


def test_fehlend_lesen_nur_belegte_punkte():
    """Die Vollständigkeitsprüfung darf keine neue Wahrheit erzeugen: Nur Punkte, die in der Grundlage stehen."""
    from app.sprachmodell import fehlend_lesen

    grundlage = ("Abschnitt 1 von 2 (0:10–44:32):\n[0:10] Die Gruppe sitzt im Kerker.\n"
                 "[44:32] Jemand löst mit einem Dolch ihre Handfesseln; sie liegen auf dem Podest.\n\n"
                 "Abschnitt 2 von 2 (1:11:39–1:11:39):\n[1:11:39] Pipo gibt der Gruppe sein Schwert als Dank.")
    d = {"fehlend": [
        {"zeit": "44:32", "notiz": "Jemand löst mit einem Dolch ihre Handfesseln."},  # fast wörtlich → belegt
        {"zeit": "1:11:39", "notiz": "Pipo gibt der Gruppe das Schwert."},  # gleiche Zeit, 4 von 6 Wörtern → belegt
        {"zeit": "0:10", "notiz": "Die Gruppe wird gefoltert und verliert alle Erinnerung."},  # erfunden → nicht belegt
        {"zeit": "2:00:00", "notiz": "Satuna erscheint."},  # Zeit gibt es nicht → nicht belegt
        {"zeit": "x", "notiz": ""}, "kaputt",
        {"zeit": "0:10", "notiz": "Kerker 6"}, {"zeit": "0:10", "notiz": "Kerker 7"}]}
    aus = fehlend_lesen(d, grundlage)
    assert len(aus) == 5  # höchstens FEHLEND_HOECHSTENS Einträge, leere und kaputte fallen weg
    assert [f["belegt"] for f in aus[:4]] == [True, True, False, False]
    assert aus[0]["zeit"] == 44 * 60 + 32 and aus[1]["zeit"] == 3600 + 11 * 60 + 39


def test_vollstaendigkeit_ergaenzt_genau_einmal_und_streicht_nichts():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            if "Schreibe Szenennotizen" in system:
                zeilen = [z for z in nutzer.split("\n") if z.startswith("[")]
                notizen = [f"{z.split(' ')[0]} Ereignis bei {z.split(' ')[0]} geschieht." for z in zeilen[::6]]
                notizen.append("[44:32] Jemand löst mit einem Dolch ihre Handfesseln.")
                return Antwort(json.dumps({"notizen": notizen}), 1, 1)
            if system.startswith("Du vergleichst den Recap"):
                return Antwort(json.dumps({"fehlend": [
                    {"zeit": "44:32", "wichtigkeit": "kritisch",
                     "notiz": "Jemand löst mit einem Dolch ihre Handfesseln."},
                    {"zeit": "0:00", "notiz": "Ein Drache greift an."}]}), 1, 1)
            if system.startswith("Du ergänzt den Recap"):
                assert "Handfesseln" in nutzer and "Drache" not in nutzer  # nur belegte Punkte
                assert "steht zwischen:" in nutzer  # Nachbarn gehen mit
                return Antwort(json.dumps({"absaetze": [
                    {"nr": 1, "text": "Die Gruppe ritt zum Ereignis. Jemand löste mit einem Dolch ihre Handfesseln."},
                    {"nr": 2, "text": "Kurz."}]}), 1, 1)  # Absatz 2 gekürzt → wird nicht übernommen
            if system.startswith("Du prüfst den Recap"):
                return Antwort(json.dumps({"absaetze": [{"nr": 1, "urteil": "belegt", "stellen": []},
                                                        {"nr": 2, "urteil": "belegt", "stellen": []}]}), 1, 1)
            if "Recap" in system:
                return Antwort(json.dumps({"title": "Kapitel 1: X", "text": "Die Gruppe ritt zum Ereignis.\n\nDann rasteten alle lange.",
                                           "openThreads": []}), 1, 1)
            return Antwort(json.dumps({"proposals": []}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k, max_transkript_tokens=3000, stueck_tokens=1500)
    ein = _ein(600)
    d = ablauf.ausfuehren(ein, ein, gegenpruefen=True)
    assert ablauf.letztes_kapitel1 == "Die Gruppe ritt zum Ereignis.\n\nDann rasteten alle lange."
    assert d["text"] == "Die Gruppe ritt zum Ereignis. Jemand löste mit einem Dolch ihre Handfesseln.\n\nDann rasteten alle lange."
    assert ablauf.letztes_kapitel2 == d["text"]
    assert [(f["belegt"], f["ergaenzt"]) for f in ablauf.letzter_befund_fehlend] == [(True, True), (False, False)]
    assert ablauf.letzter_befund_fehlend[0]["davor"].startswith("[") and ablauf.letzter_befund_fehlend[0]["wichtigkeit"] == "kritisch"
    assert sum(1 for s, _ in k.aufrufe if s.startswith("Du ergänzt")) == 1
    # Reihenfolge: Recap → Vollständigkeit → Ergänzung → Prüfung
    arten = [s.split(" ")[1] for s, _ in k.aufrufe if s.startswith("Du ")]
    assert arten[-3:] == ["vergleichst", "ergänzt", "prüfst"] or arten[-4:-1] == ["vergleichst", "ergänzt", "prüfst"]
    assert ablauf.letzte_pruefung_vorher and ablauf.letzte_pruefung_nachher == [] and d["review"]["revised"] is False
    assert "Wer gibt wem was" in [s for s, _ in k.aufrufe if s.startswith("Du prüfst")][0]


def test_ergaenzung_nur_an_passender_stelle_und_nur_wichtige():
    """Nachbarnotizen bestimmen den Absatz; passt kein Absatz, wird nicht ergänzt. Nebensächliches nie."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = []

        def chat(self, system, nutzer):
            self.nutzer.append((system, nutzer))
            return Antwort(json.dumps({"absaetze": [
                {"nr": 2, "text": "Dann rasteten alle lange. Pipo übergab sein Schwert Lostriana."}]}), 1, 1)

    k = Klient()
    ablauf = Ablauf(k)
    ein = _ein(10)
    text = "Die Gruppe kletterte aus dem Krater und fand Pipo verletzt.\n\nDann rasteten alle lange."
    fehlend = [
        {"zeit": 4299.0, "notiz": "Pipo gibt der Gruppe sein Schwert Lostriana.", "wichtigkeit": "kritisch", "belegt": True,
         "davor": "[1:10:00] Orasilas verbindet Pipo im Krater.", "danach": "[1:13:05] Die Gruppe verlässt den Krater.",
         "ergaenzt": False},
        {"zeit": 100.0, "notiz": "Arlekin schläft im Heubett.", "wichtigkeit": "nebensächlich", "belegt": True,
         "davor": "", "danach": "", "ergaenzt": False}]
    # Absatz 2 („rasteten“) erzählt die Nachbarn (Krater, Pipo) nicht → Ergänzung an falscher Stelle wird verworfen
    assert ablauf.ergaenzen(ein, text, fehlend) is None
    assert "Heubett" not in k.nutzer[0][1] and "Lostriana" in k.nutzer[0][1]  # Nebensächliches geht nicht mit
    assert fehlend[0]["ergaenzt"] is False
    # richtiger Absatz → übernommen
    k.chat = lambda system, nutzer: Antwort(json.dumps({"absaetze": [
        {"nr": 1, "text": "Die Gruppe kletterte aus dem Krater und fand Pipo verletzt. Pipo übergab ihnen sein Schwert Lostriana."}]}), 1, 1)
    neu = ablauf.ergaenzen(ein, text, fehlend)
    assert neu.startswith("Die Gruppe kletterte") and "Lostriana" in neu and fehlend[0]["ergaenzt"] is True
    # nur Nebensächliches → kein Aufruf
    assert ablauf.ergaenzen(ein, text, fehlend[1:]) is None


def test_relationen_gegen_transkript():
    """Atomare Claims sehen nur kurze Transkriptfenster; nur exakter Claim + echte Source-ID darf widersprechen."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = []

        def chat(self, system, nutzer):
            self.nutzer.append(nutzer)
            if "Absatz 1:" in nutzer:
                return Antwort(json.dumps({"claims": [{
                    "claim": "Orasilas gab Pipo sein Schwert.",
                    "urteil": "widerspricht",
                    "korrektur": "Pipo gab der Gruppe sein Schwert.",
                    "begruendung": "Pipo gibt der Gruppe das Schwert, nicht umgekehrt.",
                    "sourceIds": ["L0216"]
                }]}), 1, 1)
            return Antwort(json.dumps({"claims": [{
                "claim": "Dann rasteten alle lange.",
                "urteil": "stimmt",
                "korrektur": "",
                "begruendung": "",
                "sourceIds": []
            }]}), 1, 1)

    ein = _ein(400)
    ein["transkript"][215]["text"] = "Senke dein Schwert, Pipo. Pipo gibt der Gruppe sein Schwert."
    text = "Orasilas gab Pipo sein Schwert.\n\nDann rasteten alle lange."
    befund = [{"index": 0, "verdict": "supported", "note": None,
               "evidence": [{"start": 4300.0, "quote": "Schwert"}]},
              {"index": 1, "verdict": "supported", "note": None,
               "evidence": [{"start": 7000.0, "quote": "rasten"}]}]
    k = Klient()
    aus = Ablauf(k).relationen(ein, text, befund)
    assert len(k.nutzer) == 2
    assert "L0216 |" in k.nutzer[0] and "Senke dein Schwert, Pipo" in k.nutzer[0] and "Satz 100 " not in k.nutzer[0]
    assert "Satz 214 " in k.nutzer[0] and "Satz 218 " in k.nutzer[0] and "Satz 230 " not in k.nutzer[0]
    assert aus[0]["urteil"] == "widerspricht" and aus[0]["fenster"] == ["1:11:40"]
    assert aus[0]["claims"][0]["exakt"] is True and aus[0]["claims"][0]["zitatBelegt"] is True
    assert aus[0]["claims"][0]["gegenbelegBelegt"] is True
    assert aus[0]["claims"][0]["sourceIds"] == ["L0216"] and "gibt der Gruppe" in aus[0]["claims"][0]["zitat"]
    assert aus[1]["urteil"] == "stimmt"
    assert befund[0]["verdict"] == "contradicted" and befund[0]["relation_contradicted"] is True
    assert befund[1]["verdict"] == "supported"
    vor = len(k.nutzer)
    assert Ablauf(k).relationen(ein, text, [{"index": 0, "verdict": "supported",
                                             "note": None, "evidence": []}]) == []
    assert len(k.nutzer) == vor


def test_recap_plan_klassifiziert_alle_ids_in_kurzen_fenstern():
    """0.4.50: Keine Top-N-Auswahl mehr. Jede Notiz wird klassifiziert; ungültige IDs fliegen raus und fehlende
    gültige IDs fallen auf 'wichtig' zurück, damit der Plan keine Information still wegselektiert."""
    from app.sprachmodell import Ablauf, Antwort, PLAN_MINUTEN

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append((system, nutzer))
            assert system.startswith("Du klassifizierst die Szenennotizen")
            ids = [z.split(" |", 1)[0] for z in nutzer.splitlines() if z.startswith("N") and " |" in z]
            assert ids
            # Erste ID kritisch, letzte nebensächlich; ggf. mittlere ID absichtlich weglassen -> Fallback wichtig.
            antwort = {"critical": [ids[0], "N999"], "important": [], "minor": [ids[-1]]}
            return Antwort(json.dumps(antwort), 1, 1)

    notizen = "\n".join([
        "[0:00] Die Gruppe wird eingesperrt.",
        "[10:00] Eine Wache nennt ihren Namen.",
        "[20:00] Ein Helfer löst die Fesseln.",
        "[31:00] Kano ist tot.",
        "[40:00] Orasilas rettet Arlekin aus dem Wasser.",
        "[44:00] Die Menge flieht vom eingestürzten Platz.",
        "[50:00] Pipo übergibt Lostriana.",
        "[61:00] Die Gruppe schließt eine Abmachung.",
    ])
    k = Klient()
    ablauf = Ablauf(k)
    plan = ablauf.planen(_ein(10), notizen)

    assert PLAN_MINUTEN == 15
    assert len(plan) == 8 and {p["id"] for p in plan} == {f"N{i:03d}" for i in range(1, 9)}
    assert all(p["id"] != "N999" for p in plan)
    assert all(p["notiz"] in notizen for p in plan)
    assert len(k.aufrufe) >= 4  # 0–15, 15–30, 30–45, 45–60, 60–75; leere Fenster werden übersprungen
    # N005 liegt mit N004 im 30–45-Fenster und wird vom Modell dort ausgelassen -> sicherheitshalber wichtig.
    n5 = next(p for p in plan if p["id"] == "N005")
    assert n5["wichtigkeit"] == "wichtig" and n5["fallback"] is True
    assert {p["wichtigkeit"] for p in plan} <= {"kritisch", "wichtig", "nebensächlich"}


def test_recap_bekommt_nur_kritisch_und_wichtig_als_pflicht():
    """Nebensächliche Klassifikationen bleiben in plan.json sichtbar, werden aber nicht als Pflicht in die Prosa gedrückt."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.nutzer = ""

        def chat(self, system, nutzer):
            self.nutzer = nutzer
            return Antwort(json.dumps({"title": "Kapitel 1: Test", "text": "Die Gruppe handelt.",
                                       "openThreads": []}), 1, 1)

    plan = [
        {"id": "N001", "zeit": 0.0, "notiz": "[0:00] Kano ist tot.", "teil": 1,
         "wichtigkeit": "kritisch", "fallback": False},
        {"id": "N002", "zeit": 10.0, "notiz": "[0:10] Pipo übergibt Lostriana.", "teil": 1,
         "wichtigkeit": "wichtig", "fallback": False},
        {"id": "N003", "zeit": 20.0, "notiz": "[0:20] Die Gruppe isst Brot.", "teil": 1,
         "wichtigkeit": "nebensächlich", "fallback": False},
    ]
    k = Klient()
    Ablauf(k).recap(_ein(10), "Szenennotizen", "\n".join(p["notiz"] for p in plan), plan)
    assert "N001" in k.nutzer and "N002" in k.nutzer
    assert "N003" not in k.nutzer
    assert "[kritisch]" in k.nutzer and "[wichtig]" in k.nutzer



def test_posthoc_coverage_ergaenzt_nur_noch_kritisch():
    """Mit Pflichtplan ist post-hoc Coverage nur Sicherheitsnetz: 'wichtig' bleibt Diagnose, nicht Auto-Insert."""
    from app.sprachmodell import Ablauf

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            raise AssertionError("für nur wichtige/nebensächliche Punkte darf kein Ergänzungsaufruf erfolgen")

    fehlend = [
        {"zeit": 10.0, "notiz": "Pipo nennt seinen Namen.", "wichtigkeit": "wichtig", "belegt": True,
         "davor": "", "danach": "", "ergaenzt": False},
        {"zeit": 20.0, "notiz": "Die Gruppe schläft.", "wichtigkeit": "nebensächlich", "belegt": True,
         "davor": "", "danach": "", "ergaenzt": False},
    ]
    assert Ablauf(Klient()).ergaenzen(_ein(10), "Die Gruppe geht weiter.", fehlend) is None


def _relations_patch_klient(zweiter_befund: str):
    from app.sprachmodell import Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.relationsaufrufe = 0
            self.systeme = []

        def chat(self, system, nutzer):
            self.systeme.append(system)
            if system.startswith("Du prüfst den Recap"):
                return Antwort(json.dumps({"absaetze": [{
                    "nr": 1, "urteil": "belegt",
                    "stellen": [{"zeit": "1:11:40", "zitat": "Nehmt es"}],
                    "begruendung": ""
                }]}), 1, 1)
            if system.startswith("Du prüfst genau EINEN Absatz"):
                self.relationsaufrufe += 1
                if "Orasilas gab Pipo sein Schwert." in nutzer:
                    return Antwort(json.dumps({"claims": [{
                        "claim": "Orasilas gab Pipo sein Schwert.",
                        "urteil": "widerspricht",
                        "korrektur": "Pipo gab der Gruppe sein Schwert.",
                        "begruendung": "Die Übergabe läuft von Pipo zur Gruppe.",
                        "sourceIds": ["L0216"]
                    }]}), 1, 1)
                return Antwort(json.dumps({"claims": [{
                    "claim": "Pipo gab der Gruppe sein Schwert.",
                    "urteil": zweiter_befund,
                    "korrektur": "",
                    "begruendung": "",
                    "sourceIds": ["L0216"]
                }]}), 1, 1)
            if system.startswith("Du überarbeitest einzelne Absätze"):
                raise AssertionError("Relationsfehler darf keinen ganzen Absatz neu generieren")
            raise AssertionError("unerwarteter Modellaufruf")

    return Klient()


def test_relationspatch_aendert_nur_claim_und_erhaelt_rest():
    from app.sprachmodell import Ablauf

    ein = _ein(400)
    ein["transkript"][215]["text"] = "Nehmt es. Pipo reicht der Gruppe sein Schwert."
    original = ("Orasilas hatte zuvor die Vision. Orasilas gab Pipo sein Schwert. "
                "Danach verließ die Gruppe den Krater.")
    r = {"text": original}
    k = _relations_patch_klient("stimmt")
    ablauf = Ablauf(k)
    review = ablauf.gegenpruefen(ein, "Szenennotizen",
                                 "[1:11:40] Pipo gibt der Gruppe sein Schwert.", r)
    assert review["revised"] is True
    assert r["text"] == ("Orasilas hatte zuvor die Vision. Pipo gab der Gruppe sein Schwert. "
                         "Danach verließ die Gruppe den Krater.")
    assert "Orasilas hatte zuvor die Vision." in r["text"] and "Danach verließ die Gruppe den Krater." in r["text"]
    assert k.relationsaufrufe == 2
    assert ablauf.letzte_relationen_vorher[0]["claims"][0]["gepatcht"] is True


def test_relationspatch_wird_bei_unsicherer_nachpruefung_zurueckgenommen():
    from app.sprachmodell import Ablauf

    ein = _ein(400)
    ein["transkript"][215]["text"] = "Nehmt es. Pipo reicht der Gruppe sein Schwert."
    original = ("Orasilas hatte zuvor die Vision. Orasilas gab Pipo sein Schwert. "
                "Danach verließ die Gruppe den Krater.")
    r = {"text": original}
    k = _relations_patch_klient("unklar")
    ablauf = Ablauf(k)
    review = ablauf.gegenpruefen(ein, "Szenennotizen",
                                 "[1:11:40] Pipo gibt der Gruppe sein Schwert.", r)
    assert review["revised"] is False
    assert r["text"] == original
    claim = ablauf.letzte_relationen_vorher[0]["claims"][0]
    assert claim["gepatcht"] is False and claim["zurueckgenommen"] is True
    assert not any(s.startswith("Du überarbeitest einzelne Absätze") for s in k.systeme)


def test_relationen_patchen_nicht_bei_blobem_fehlenden_beleg_im_fenster():
    """0.4.51: Eine echte Source-ID reicht nicht. Ohne positiven Gegenbeleg ist 'widerspricht' nur 'unklar'."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            return Antwort(json.dumps({"claims": [{
                "claim": "Orasilas hatte Besuch von seiner Schwester.",
                "urteil": "widerspricht",
                "korrektur": "Orasilas hatte keinen Besuch.",
                "begruendung": "Im Ausschnitt ist kein Besuch zu sehen.",
                "sourceIds": ["L0216"],
            }]}), 1, 1)

    ein = _ein(400)
    ein["transkript"][215]["text"] = "Orasilas sitzt seit zehn Tagen im Kerker."
    text = "Orasilas hatte Besuch von seiner Schwester."
    befund = [{"index": 0, "verdict": "supported", "note": None,
               "evidence": [{"start": 4300.0, "quote": "Kerker"}]}]
    rel = Ablauf(Klient()).relationen(ein, text, befund)
    claim = rel[0]["claims"][0]
    assert claim["zitatBelegt"] is True and claim["gegenbelegBelegt"] is False
    assert claim["urteil"] == "unklar" and rel[0]["urteil"] == "unklar"
    assert befund[0]["verdict"] == "supported"
    assert Ablauf(Klient()).relationen_patchen(text, rel) is None


def test_gegenpruefung_wird_in_kleine_bloecke_geteilt_und_faellt_nur_lokal_aus():
    """0.4.51: Eine abgeschnittene strukturierte Antwort darf nicht mehr den kompletten Review verwerfen."""
    from app.sprachmodell import Ablauf, Antwort, PRUEF_BATCH

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = []

        def chat(self, system, nutzer):
            self.aufrufe.append(nutzer)
            if "P5" in nutzer:  # mittleres Paket: beide Harness-Versuche werden abgeschnitten
                return Antwort('{"absaetze": [', 1, 1, done_reason="length")
            n = nutzer.count("Absatz ")
            return Antwort(json.dumps({"absaetze": [
                {"nr": i + 1, "urteil": "belegt", "stellen": [], "begruendung": ""} for i in range(n)
            ]}), 1, 1)

    assert PRUEF_BATCH == 4
    text = "\n\n".join(f"P{i}: belegter Absatz." for i in range(1, 10))
    k = Klient()
    befund = Ablauf(k).pruefen(_ein(2), "Transkript", "[0:00] Grundlage", text)
    assert len(befund) == 9
    assert [b["verdict"] for b in befund[:4]] == ["supported"] * 4
    assert [b["verdict"] for b in befund[4:8]] == ["unchecked"] * 4
    assert befund[8]["verdict"] == "supported"
    assert len(k.aufrufe) == 4  # 1. Paket, mittleres Paket + Repair, letztes Paket


def _ledger_event(source_ids, summary, *, subject="Alrik", value="alive", epistemic="observed"):
    return {
        "sourceIds": source_ids,
        "summary": summary,
        "kinds": ["state_change"],
        "actors": [subject],
        "targets": [],
        "objects": [],
        "locations": [],
        "factions": [],
        "assertions": [{
            "subject": subject,
            "property": "life_status",
            "value": value,
            "epistemic": epistemic,
            "certainty": "high",
        }],
        "epistemic": epistemic,
        "modality": "actual",
        "importance": "critical",
        "relevance": {"recap": True, "openThread": False, "bible": True},
        "tags": [],
    }


def test_ledger_verwirft_erfundene_source_ids_und_loest_echte_belege_auf():
    from app.sprachmodell import Ablauf, Antwort, SYSTEM_LEDGER_EVENTS, transkript_zeilen_mit_ids

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            return Antwort(json.dumps({"events": [
                _ledger_event(["L0001"], "Alrik erscheint lebendig."),
                _ledger_event(["L9999"], "Ein erfundener Beleg."),
            ]}), 1, 1)

    ein = _ein(2)
    ein["transkript"][0]["text"] = "Alrik steht plötzlich lebendig vor euch."
    events = Ablauf(Klient())._ledger_pass(
        ein, SYSTEM_LEDGER_EVENTS, transkript_zeilen_mit_ids(ein["transkript"]))
    assert len(events) == 1 and events[0]["sourceIds"] == ["L0001"]
    assert events[0]["evidence"][0]["sourceId"] == "L0001"
    assert "Alrik steht plötzlich lebendig" in events[0]["evidence"][0]["text"]


def test_ledger_state_history_unterscheidet_revision_von_echtem_zustandswechsel():
    from app.sprachmodell import Ablauf

    geglaubt_tot = _ledger_event(["L0001"], "Alrik gilt als tot.", value="dead", epistemic="believed")
    geglaubt_tot.update(eventId="E0001", time=10.0)
    lebendig = _ledger_event(["L0002"], "Alrik erscheint lebendig.", value="alive", epistemic="observed")
    lebendig.update(eventId="E0002", time=20.0)
    states = Ablauf._ledger_states([geglaubt_tot, lebendig])
    hist = states[0]["history"]
    assert hist[0]["resolution"] == "initial" and hist[0]["resolvedBy"] == "E0002"
    assert hist[1]["resolution"] == "revision" and states[0]["current"]["value"] == "alive"

    wirklich_tot = _ledger_event(["L0001"], "Alrik stirbt.", value="dead", epistemic="observed")
    wirklich_tot.update(eventId="E0001", time=10.0)
    wieder_da = _ledger_event(["L0002"], "Alrik lebt wieder.", value="alive", epistemic="observed")
    wieder_da.update(eventId="E0002", time=20.0)
    states = Ablauf._ledger_states([wirklich_tot, wieder_da])
    assert states[0]["history"][1]["resolution"] == "state_change"


def test_ledger_historie_wird_nur_fuer_aktuelle_entitaeten_geholt_und_ids_validiert():
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            assert system.startswith("Du ordnest aktuelle")
            return Antwort(json.dumps({"links": [{
                "eventId": "E0001",
                "relation": "revises",
                "historyIds": ["H0001", "H9999"],
                "reason": "Der frühere Todesstatus wird durch das lebendige Auftauchen revidiert.",
            }]}), 1, 1)

    ein = _ein(1)
    ein["bibel"] = [
        {"id": "a", "typ": "npc", "name": "Alrik", "zusammenfassung": "Galt zuletzt als tot."},
        {"id": "b", "typ": "npc", "name": "Berta", "zusammenfassung": "Lebt in Havena."},
    ]
    ein["_historie"] = [
        {"session": 4, "title": "Der Fall", "text": "Die Gruppe hielt Alrik für tot.", "openThreads": []},
        {"session": 5, "title": "Markt", "text": "Berta kaufte Brot.", "openThreads": []},
    ]
    ev = _ledger_event(["L0001"], "Alrik erscheint lebendig.")
    ev["eventId"] = "E0001"
    ablauf = Ablauf(Klient())
    sources, event_map = ablauf._ledger_history_sources(ein, [ev])
    assert sources and all("Berta" not in x["text"] for x in sources)
    assert set(event_map) == {"E0001"}
    links = ablauf._ledger_history_links(ein, [ev], sources, event_map)
    assert links == [{
        "eventId": "E0001",
        "relation": "revises",
        "historyIds": ["H0001"],
        "reason": "Der frühere Todesstatus wird durch das lebendige Auftauchen revidiert.",
    }]


def test_ledger_truncation_teilt_nur_betroffenen_quellblock_statt_ihn_zu_verlieren():
    """0.4.52: Nach zwei length-Antworten wird der Ledger-Block halbiert und beide Hälften werden weiter verarbeitet."""
    from app.sprachmodell import Ablauf, Antwort, SYSTEM_LEDGER_EVENTS, transkript_zeilen_mit_ids

    class Klient:
        modell = "test"

        def __init__(self):
            self.aufrufe = 0

        def chat(self, system, nutzer):
            self.aufrufe += 1
            ids = re.findall(r"(?m)^(L\d{4,6}) \|", nutzer)
            if len(ids) > 3:
                return Antwort('{"events":[', 1, 1, done_reason="length")
            return Antwort(json.dumps({"events": [
                _ledger_event([ids[0]], f"Ereignis {ids[0]}.")
            ]}), 1, 1)

    ein = _ein(8)
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    k = Klient()
    events = Ablauf(k)._ledger_pass(ein, SYSTEM_LEDGER_EVENTS, zeilen)
    assert len(events) >= 2
    assert {e["sourceIds"][0] for e in events}.issubset({lid for lid, _ in zeilen})
    assert k.aufrufe >= 4  # voller Block: 2 Reparaturversuche, danach mindestens zwei erfolgreiche Teilblöcke


def test_ledger_review_kann_widerspruechliche_kandidaten_mergen_und_richtung_reparieren():
    """0.4.52: Zwei Pässe dürfen nicht als widersprüchliche Wahrheit nebeneinander stehen bleiben."""
    from app.sprachmodell import Ablauf, Antwort, transkript_zeilen_mit_ids

    replacement = {
        "sourceIds": ["L0001"],
        "summary": "Pipo gibt der Gruppe sein Schwert.",
        "kinds": ["possession"],
        "actors": ["Pipo"],
        "targets": ["Gruppe"],
        "objects": ["Schwert"],
        "locations": [],
        "factions": [],
        "assertions": [{
            "subject": "Schwert", "property": "possession", "value": "bei der Gruppe",
            "epistemic": "observed", "certainty": "high",
        }],
        "epistemic": "observed",
        "modality": "actual",
        "importance": "critical",
        "relevance": {"recap": True, "openThread": False, "bible": True},
        "tags": [],
    }

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            assert system.startswith("Du prüfst Ledger-Kandidaten")
            return Antwort(json.dumps({"reviews": [{
                "originIds": ["C0001", "C0002"],
                "verdict": "merge",
                "reason": "Beide Kandidaten beschreiben dieselbe Übergabe; einer hatte die Richtung vertauscht.",
                "replacement": replacement,
            }], "coverage": [], "anchors": [], "encounters": []}), 1, 1)

    ein = _ein(2)
    ein["transkript"][0]["sprecher"] = "Spielleitung"
    ein["transkript"][0]["text"] = "Pipo reicht euch sein Schwert. Nehmt es."
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    falsch = {**replacement, "summary": "Die Gruppe gibt Pipo das Schwert.", "actors": ["Gruppe"],
              "targets": ["Pipo"], "candidateId": "C0001", "extractionPass": "events"}
    richtig = {**replacement, "candidateId": "C0002", "extractionPass": "continuity"}
    events, diag, anchors, encounters = Ablauf(Klient())._ledger_review(ein, [falsch, richtig], zeilen)
    assert len(events) == 1
    assert events[0]["actors"] == ["Pipo"] and events[0]["targets"] == ["Gruppe"]
    assert events[0]["_review"]["verdict"] == "merge"
    assert diag["merged"] == 1 and anchors == [] and encounters == []


def test_ledger_review_kann_vollstaendig_fehlenden_kritischen_status_nachtragen():
    """0.4.53: Derselbe Quellen-Review schließt kritische Coverage-Lücken, auch wenn der Primärpass nichts fand."""
    from app.sprachmodell import Ablauf, Antwort, transkript_zeilen_mit_ids

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            assert system.startswith("Du prüfst Ledger-Kandidaten")
            assert "KANDIDATEN:\n(keine)" in nutzer
            return Antwort(json.dumps({"reviews": [], "coverage": [
                _ledger_event(["L0001"], "Kano stirbt.", subject="Kano", value="dead", epistemic="observed")
            ], "anchors": [], "encounters": []}), 1, 1)

    ein = _ein(2)
    ein["transkript"][0]["text"] = "Kano bricht tot zusammen."
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    events, diag, anchors, encounters = Ablauf(Klient())._ledger_review(ein, [], zeilen)
    assert len(events) == 1
    assert events[0]["assertions"][0]["subject"] == "Kano"
    assert events[0]["assertions"][0]["value"] == "dead"
    assert events[0]["_review"]["verdict"] == "coverage_added"
    assert diag["coverageAdded"] == 1 and diag["reviewCalls"] == 1
    assert anchors == [] and encounters == []


def test_ledger_054_hat_keinen_zweiten_extraktions_oder_separaten_coverage_pass():
    """0.4.54: Primärextraktion + kombinierter Review/Anchors/Encounter; kein zusätzlicher Vollpass."""
    from app.sprachmodell import Ablauf, Antwort

    class Klient:
        modell = "test"

        def __init__(self):
            self.systeme = []

        def chat(self, system, nutzer):
            self.systeme.append(system)
            if system.startswith("Du extrahierst ein Ereignis-Ledger"):
                return Antwort(json.dumps({"events": []}), 1, 1)
            if system.startswith("Du prüfst Ledger-Kandidaten"):
                return Antwort(json.dumps({"reviews": [], "coverage": [], "anchors": [], "encounters": []}), 1, 1)
            raise AssertionError(f"unerwarteter Ledger-Pass: {system[:80]}")

    k = Klient()
    ledger = Ablauf(k).ledger(_ein(2))
    assert ledger["state"] == "ok"
    assert sum(s.startswith("Du extrahierst ein Ereignis-Ledger") for s in k.systeme) == 1
    assert sum(s.startswith("Du prüfst Ledger-Kandidaten") for s in k.systeme) == 1
    assert not any(s.startswith("Du extrahierst aus EINEM Abschnitt") for s in k.systeme)
    assert not any(s.startswith("Du suchst im ORIGINALTRANSKRIPT") for s in k.systeme)


def test_ledger_055_anchor_ist_nur_pruefhinweis_und_ueberschreibt_nichts_lokal():
    """0.4.55: Ein widersprechender Anchor darf ohne Micro-Review niemals selbst zur Wahrheit werden."""
    from app.sprachmodell import Ablauf, transkript_zeilen_mit_ids

    ein = _ein(1)
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    event = _ledger_event(["L0001"], "Das Artefakt bleibt bei der Besitzerin.", subject="Artefakt",
                           value="bei der Besitzerin", epistemic="observed")
    event["assertions"][0]["property"] = "possession"
    event["_review"] = {"verdict": "accepted", "originIds": ["C0001"], "reason": ""}
    anchor = {"originIds": ["C0001"], "sourceIds": ["L0001"], "subject": "Artefakt", "property": "possession",
              "value": "bei der Gruppe", "epistemic": "observed", "certainty": "high", "importance": "critical",
              "chunk": 0}
    events, diag = Ablauf.__new__(Ablauf)._ledger_integrity([event], [anchor], zeilen)
    assert events[0]["assertions"][0]["value"] == "bei der Besitzerin"
    assert diag["anchorAdded"] == 0 and diag["anchorConflicts"] == 1


def test_ledger_055_tischrolle_wird_sanitized_wenn_weltfakt_erhalten_bleibt():
    """Spielleitung/Game Master verschwindet als Entity, ein unabhängiger belegter Weltfakt bleibt erhalten."""
    from app.sprachmodell import Ablauf, transkript_zeilen_mit_ids

    ein = _ein(1)
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    e = _ledger_event(["L0001"], "Spielleitung (Wachmann) bringt die Mahlzeit.", subject="Wachmann",
                       value="Bringer der Mahlzeit")
    e["assertions"][0]["property"] = "role_status"
    e["actors"] = ["Spielleitung (Wachmann)"]
    e["_review"] = {"verdict": "accepted", "originIds": ["C0001"], "reason": ""}
    events, diag = Ablauf.__new__(Ablauf)._ledger_integrity([e], [], zeilen)
    assert len(events) == 1 and events[0]["actors"] == []
    assert "Spielleitung" not in events[0]["summary"]
    assert events[0]["assertions"][0]["subject"] == "Wachmann"
    assert "table_role_sanitized" in events[0]["tags"]
    assert diag["metaSanitized"] == 1 and diag["metaRejected"] == 0


def test_ledger_055_encounter_fragmente_werden_systemagnostisch_verbunden():
    from app.sprachmodell import Ablauf

    fragmente = [
        {"chunk": 2, "sourceIds": ["L0100"], "kind": "combat", "boundary": "start",
         "participants": ["Team", "Wachen"], "locations": ["Lager"], "objectives": ["Person befreien"],
         "domains": ["Haupthalle"], "summary": "Der Kampf beginnt.", "turningPoints": [], "outcomes": [],
         "consequences": [], "unresolved": []},
        {"chunk": 3, "sourceIds": ["L0150"], "kind": "combat", "boundary": "middle",
         "participants": ["Team", "Wachen"], "locations": ["Lager"], "objectives": ["Person befreien"],
         "domains": ["Nebentrakt"], "summary": "Verstärkung drängt das Team zurück.",
         "turningPoints": ["Verstärkung trifft ein."], "outcomes": [], "consequences": [], "unresolved": []},
        {"chunk": 4, "sourceIds": ["L0200"], "kind": "combat", "boundary": "end",
         "participants": ["Team", "Wachen"], "locations": ["Lager"], "objectives": ["Person befreien"],
         "domains": ["Ausgang"], "summary": "Das Team entkommt.", "turningPoints": [],
         "outcomes": ["Die Zielperson wird befreit."], "consequences": ["Das Team wird verfolgt."],
         "unresolved": ["Eine Wache entkommt."]},
    ]
    encounters = Ablauf._ledger_encounters(fragmente)
    assert len(encounters) == 1
    assert len(encounters[0]["phases"]) == 3
    assert encounters[0]["participants"] == ["Team", "Wachen"]
    assert encounters[0]["outcomes"][0]["text"] == "Die Zielperson wird befreit."


def test_ledger_055_irrelevantes_event_faellt_schon_beim_normalisieren_weg():
    from app.sprachmodell import Ablauf

    e = _ledger_event(["L0001"], "Belanglose Routine.")
    e["relevance"] = {"recap": False, "openThread": False, "bible": False}
    assert Ablauf.__new__(Ablauf)._ledger_event_normalisieren(e, {"L0001"}, {"L0001": "[0:00] X: Routine"}) is None


def test_ledger_055_anchor_microreview_nutzt_gespraechsrolle_bei_falschem_speakerlabel():
    """Goldfall Kano: Frage nach NPC-Zustand + unmittelbare autoritative Antwort trotz falschem Diarisierungslabel."""
    from app.sprachmodell import Ablauf, Antwort, transkript_zeilen_mit_ids

    class Klient:
        modell = "test"

        def chat(self, system, nutzer):
            assert system.startswith("Du löst NUR wenige strittige")
            assert "Was tut der Kano?" in nutzer and "Der ist tot." in nutzer
            event = _ledger_event(["L0001", "L0002"], "Kano ist tot.", subject="Kano",
                                  value="dead", epistemic="observed")
            event["tags"] = ["speaker_conflict"]
            return Antwort(json.dumps({"resolutions": [{
                "anchorId": "A0001", "verdict": "confirmed", "event": event,
                "reason": "Die unmittelbare Antwort hat die Gesprächsrolle einer autoritativen Weltantwort."
            }]}), 1, 1)

    ein = _ein(2)
    ein["transkript"][0]["sprecher"] = "Lysander"
    ein["transkript"][0]["text"] = "Ich gucke nach Kano. Was tut der Kano?"
    ein["transkript"][1]["sprecher"] = "Orasilas"  # absichtlich falsches Whisper-Speakerlabel
    ein["transkript"][1]["text"] = "Der ist tot."
    zeilen = transkript_zeilen_mit_ids(ein["transkript"])
    anchor = {"originIds": [], "sourceIds": ["L0002"], "subject": "Kano", "property": "life_status",
              "value": "dead", "epistemic": "observed", "certainty": "high", "importance": "critical", "chunk": 0}
    events, diag = Ablauf(Klient())._ledger_anchor_resolve(ein, [], [anchor], zeilen)
    assert len(events) == 1
    assert events[0]["assertions"][0]["subject"] == "Kano"
    assert events[0]["assertions"][0]["value"] == "dead"
    assert "speaker_conflict" in events[0]["tags"]
    assert diag["calls"] == 1 and diag["confirmed"] == 1 and diag["added"] == 1


def test_ledger_055_gleiche_teilnehmer_allein_verbinden_keine_encounters():
    from app.sprachmodell import Ablauf

    fragmente = [
        {"chunk": 1, "sourceIds": ["L0100"], "kind": "conflict", "boundary": "middle",
         "participants": ["Gruppe", "Wachen"], "locations": ["Hof"], "objectives": ["Gefangenen befreien"],
         "domains": [], "summary": "Die Gruppe kämpft um den Gefangenen.", "turningPoints": ["Tor fällt."],
         "outcomes": [], "consequences": [], "unresolved": []},
        {"chunk": 2, "sourceIds": ["L0150"], "kind": "conflict", "boundary": "middle",
         "participants": ["Gruppe", "Wachen"], "locations": ["Hof"], "objectives": ["Archiv durchsuchen"],
         "domains": [], "summary": "Später streitet die Gruppe um das Archiv.", "turningPoints": ["Alarm ertönt."],
         "outcomes": [], "consequences": [], "unresolved": []},
    ]
    encounters = Ablauf._ledger_encounters(fragmente)
    assert len(encounters) == 2


def test_ledger_bibelhistorie_nicht_mehr_nur_wegen_gleichem_namen():
    """0.4.52: Eine beliebige Handlung von Lysander darf nicht seine Rollenbeschreibung als History-Link anbieten."""
    from app.sprachmodell import Ablauf

    ein = _ein(1)
    ein["bibel"] = [{"id": "l", "typ": "npc", "name": "Lysander", "zusammenfassung": "berühmter Schriftsteller"}]
    ziel = {
        "eventId": "E0001", "sourceIds": ["L0001"], "summary": "Lysander verspricht Pipo eine Ode.",
        "actors": ["Lysander"], "targets": ["Pipo"], "objects": [], "locations": [], "factions": [],
        "assertions": [{"subject": "Lysander", "property": "goal", "value": "Ode schreiben",
                        "epistemic": "stated", "certainty": "high"}],
    }
    sources, event_map = Ablauf._ledger_history_sources(Ablauf.__new__(Ablauf), ein, [ziel])
    assert sources == [] and event_map == {}

    ident = {**ziel, "assertions": [{"subject": "Lysander", "property": "identity", "value": "Schriftsteller",
                                     "epistemic": "stated", "certainty": "high"}]}
    ablauf = Ablauf.__new__(Ablauf)
    sources, event_map = ablauf._ledger_history_sources(ein, [ident])
    assert len(sources) == 1 and event_map == {"E0001": ["H0001"]}
