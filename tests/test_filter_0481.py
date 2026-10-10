"""Server 0.4.81: Die Textfilter beschädigen keinen richtigen Text mehr – „Du“ als Name, schließende Anführungszeichen beim
Teilen langer Absätze, gleich gebaute Sätze über verschiedene Personen."""
from app import artefakte
from app import sprachmodell as sm


def test_du_als_name_mit_verb_in_dritter_person():
    text = ("Du erhöhte den Preis auf 800 Silberdollar. Du stürzte, und aus seinem Mund krochen Aale. "
            "Du ist der Anführer der Bande. Du siehst einen Mann am Pier. Du gingst zur Tür. Du, pass auf.")
    aus, befunde = artefakte.kapitel(text, [], [])
    assert aus == "Du erhöhte den Preis auf 800 Silberdollar. Du stürzte, und aus seinem Mund krochen Aale. Du ist der Anführer der Bande."
    assert [b["art"] for b in befunde] == ["du_form", "du_form", "du_form"]
    # wie bisher: kleingeschriebene Anrede, „Du Hanlin …“ als Name
    assert artefakte.kapitel("Ana sah, dass du müde warst.", [], [])[0] == ""
    assert artefakte.kapitel("Du Hanlin lachte laut.", [], [])[0] == "Du Hanlin lachte laut."


def test_absatz_teilen_behaelt_anfuehrungszeichen():
    saetze = [f"Satz {i} erzählt etwas über den Abend am Hafen und die Leute dort." for i in range(30)]
    saetze[8] = "Prato rief: „Kommt rüber.“"
    saetze[9] = "Er lachte: „Kommen Sie mal rüber. Ich glaube, auf Sie haben wir gewartet. Setzen Sie sich.“"
    roh = " ".join(saetze)
    aus = sm.absaetze_teilen(roh)
    assert aus.count("„") == aus.count("“") == roh.count("“") == 2
    assert aus.replace("\n\n", " ") == roh  # kein Wort, kein Zeichen verändert
    for absatz in aus.split("\n\n"):
        assert absatz.count("„") == absatz.count("“")  # nie mitten in wörtlicher Rede geteilt


def test_gleich_gebaute_saetze_ueber_verschiedene_personen_bleiben():
    text = ("Matteo übergab ihr eine Visitenkarte mit dem Treffpunkt für den nächsten Abend. Später ging sie heim. "
            "Dan übergab ihnen eine Visitenkarte mit dem Treffpunkt für den nächsten Abend.")
    aus, befunde = artefakte.kapitel(text, [], [])
    assert aus == text and befunde == []
    # eine echte Wiederholung fällt weiter weg
    doppelt = ("Matteo übergab ihr eine Visitenkarte mit dem Treffpunkt für den nächsten Abend. Später ging sie heim. "
               "Matteo übergab ihr die Visitenkarte mit dem Treffpunkt für den nächsten Abend.")
    aus, befunde = artefakte.kapitel(doppelt, [], [])
    assert aus.count("Visitenkarte") == 1 and [b["art"] for b in befunde] == ["wiederholung"]
