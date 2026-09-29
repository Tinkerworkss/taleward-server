"""Qualitätsprüfung Stufe 1: Belege der Vorschläge gegen das Transkript."""
import time

from app import belege

SEGMENTE = [
    (10.0, "Also, ihr kommt in Rabenfels an. Es regnet."),
    (15.5, "Der Wirt heißt Borin Eisenfaust und schenkt euch Met aus."),
    (22.0, "Ich frage ihn nach dem Grauen Fürsten."),
    (30.0, "Der Graue Fürst? Den hat hier seit Jahren niemand gesehen."),
    (95.0, "Wer will noch Pizza?"),
]


def t():
    return belege.Transkript(SEGMENTE)


def test_genaues_zitat_mit_echtem_wortlaut():
    assert belege.pruefen(t(), [{"start": 15.5, "quote": "der wirt heisst Borin Eisenfaust"}]) == [
        {"start": 15.5, "quote": "Der Wirt heißt Borin Eisenfaust"}]


def test_erkennungsfehler_und_satzzeichen_werden_toleriert():
    g = belege.pruefen(t(), [{"start": 30.0, "quote": "Den hat hier seit Jahren niemand mehr gesehen"}])
    assert g and g[0]["start"] == 30.0 and g[0]["quote"].startswith("Den hat hier")


def test_falscher_zeitpunkt_wird_korrigiert():
    assert belege.pruefen(t(), [{"start": 900.0, "quote": "Ich frage ihn nach dem Grauen Fürsten"}]) == [
        {"start": 22.0, "quote": "Ich frage ihn nach dem Grauen Fürsten."}]


def test_zitat_ueber_zwei_segmente():
    g = belege.pruefen(t(), [{"start": 22.0, "quote": "nach dem Grauen Fürsten. Der Graue Fürst?"}])
    assert g and g[0]["start"] == 22.0


def test_erfundenes_zitat_faellt_weg():
    assert belege.pruefen(t(), [{"start": 15.0, "quote": "Borin verrät, dass der Fürst im Keller wohnt"},
                                {"start": 0, "quote": ""}, "kein dict"]) == []


def test_kurze_zitate_nur_wortgleich():
    assert belege.pruefen(t(), [{"start": 95.0, "quote": "Pizza"}]) == [{"start": 95.0, "quote": "Pizza?"}]
    assert belege.pruefen(t(), [{"start": 95.0, "quote": "Pasta"}]) == []


def test_doppelte_belege_nur_einmal():
    b = {"start": 15.5, "quote": "Borin Eisenfaust"}
    assert len(belege.pruefen(t(), [b, dict(b)])) == 1


def test_leeres_transkript():
    assert belege.pruefen(belege.Transkript([]), [{"start": 1.0, "quote": "irgendwas hier gesagt"}]) == []


def test_lange_session_bleibt_schnell():
    """4 Stunden, etwa 3000 Segmente: 12 Vorschläge mit je 3 Belegen in wenigen Sekunden."""
    woerter = ("ich gehe zur tür und schaue ob jemand da ist dann würfle ich auf wahrnehmung der ork greift an "
               "wir laufen weg zum fluss die brücke ist kaputt").split()
    segmente = [(i * 4.8, " ".join(woerter[(i * 7 + k) % len(woerter)] for k in range(14))) for i in range(3000)]
    tr = belege.Transkript(segmente)
    zitate = [{"start": 600.0, "quote": "der ork greift an wir laufen weg zum fluss"},
              {"start": 50.0, "quote": "Mirabella beschwört einen Drachen aus Kristall"},
              {"start": 9000.0, "quote": "die brücke ist kaputt ich gehe zur tür"}] * 12
    anfang = time.monotonic()
    for i in range(12):
        belege.pruefen(tr, zitate[i * 3:i * 3 + 3])
    assert time.monotonic() - anfang < 10
