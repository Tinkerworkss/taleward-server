"""Server 0.4.80: Teile nur nach einem Zeitsprung (Fenster 25–75 %), Absatzzahl passend zur Wortvorgabe, Kürzen als
„streiche etwa N Wörter“ – nie unter die Untergrenze und nie ohne das Ende des Kapitels."""
from app import sprachmodell as sm

NOTIZEN = "\n".join(f"[{m // 60}:{m % 60:02d}:00] Szene bei Minute {m}." for m in range(0, 430, 2))
DAUER = 429 * 60.0


def test_zeitsprung_im_weiteren_fenster():
    d = {"titel_anfang": "Ankunft", "einschnitte": [
        {"zeit": "1:58:00", "art": "zeitsprung", "zitat": "Szene bei Minute 118", "titel": "Eine Woche später"}]}
    teile = sm.einschnitte_lesen(d, NOTIZEN, DAUER, 2)
    assert [t["von"] for t in teile] == [0.0, 118 * 60.0] and teile[1]["titel"] == "Eine Woche später"
    # vor 25 % der Runde: kein Einschnitt
    d["einschnitte"][0].update(zeit="1:40:00", zitat="Szene bei Minute 100")
    assert sm.einschnitte_lesen(d, NOTIZEN, DAUER, 2) == []
    assert sm.TEILE_FENSTER == (0.25, 0.75)


def test_absatzzahl_trifft_die_mitte():
    assert sm.absatzzahl(1800, 2400) == 14 and sm.absatzzahl(900, 1500) == 8 and sm.absatzzahl(250, 600) == 4
    assert "je Absatz etwa 150 Wörter" not in sm.SYSTEM_RECAP  # steht in der Längenvorgabe, nicht in der Anweisung


def test_ende_erhalten():
    lang = ("Die Gruppe kam in der Stadt an.\n\nSie verhandelte mit dem Händler.\n\n"
            "Am Bahnhof trennten sich alle und verabredeten sich für morgen in der Garage.")
    gut = "Die Gruppe kam an.\n\nAm Bahnhof trennten sich alle und verabredeten sich für morgen in der Garage."
    abgeschnitten = "Die Gruppe kam in der Stadt an.\n\nSie verhandelte mit dem Händler."
    zusammengefasst = "Die Gruppe kam an und verhandelte.\n\nDann ging jeder nach Hause."
    assert sm.ende_erhalten(lang, gut) and not sm.ende_erhalten(lang, abgeschnitten)
    assert not sm.ende_erhalten(lang, zusammengefasst) and not sm.ende_erhalten(lang, "")


class Kuerzer:
    """Liefert erst eine zu starke Kürzung, dann eine passende; die Pflichtprüfung findet alles erzählt."""
    modell = "m"

    def __init__(self, antworten):
        self.antworten, self.aufrufe = list(antworten), []

    def chat(self, system, nutzer):
        import json

        self.aufrufe.append((system, nutzer))
        if system.startswith("Du kürzt das Kapitel"):
            return sm.Antwort(json.dumps({"text": self.antworten.pop(0)}), 1, 1)
        return sm.Antwort(json.dumps({"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1,
                                                    "zitat": "Ana traf Bo am Hafen"}]}), 1, 1)


def _absatz(i: int, n: int = 150) -> str:
    return " ".join(f"Wort{i}x{j}" for j in range(n)) + "."


def test_kuerzen_streicht_n_woerter_und_behaelt_das_ende():
    ende = "Am Bahnhof trennten sich alle und verabredeten sich für morgen in der Garage."
    lang = "\n\n".join(["Ana traf Bo am Hafen. " + _absatz(0)] + [_absatz(i) for i in range(1, 21)] + [ende])
    zu_kurz = "\n\n".join(["Ana traf Bo am Hafen. " + _absatz(0)] + [_absatz(i) for i in range(1, 6)] + [ende])
    passend = "\n\n".join(["Ana traf Bo am Hafen. " + _absatz(0)] + [_absatz(i) for i in range(1, 15)] + [ende])
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [], "bibel": [], "transkript": [{"start": 429 * 60.0}]}
    k = Kuerzer([zu_kurz, passend])
    a = sm.Ablauf(k, notizen_zuerst=True)
    r = {"text": lang}
    punkte = [{"zeit": 60.0, "ereignis": "Ana trifft Bo am Hafen.", "ausgang": "", "rang": "kritisch"}]
    befund = [{"nr": 1, "status": "erzaehlt", "absatz": 0, "zitat": "Ana traf Bo am Hafen"}]
    neu = a.kuerzen(ein, punkte, r, befund)
    assert neu is not None and a.letzte_kuerzung["angenommen"] is True and r["text"] == passend
    system, erster = k.aufrufe[0]
    n = len(lang.split())
    assert f"Es hat {n} Wörter" in system and f"Streiche etwa {n - 2400} Wörter" in system and "Das Ende" in system
    assert "du hast" in k.aufrufe[1][1] and a.letzte_kuerzung["zu_kurz"] == len(zu_kurz.split())
    # ohne Ende: verworfen, die lange Fassung bleibt
    ohne_ende = "\n\n".join(["Ana traf Bo am Hafen. " + _absatz(0)] + [_absatz(i) for i in range(1, 16)])
    a2 = sm.Ablauf(Kuerzer([ohne_ende]), notizen_zuerst=True)
    r2 = {"text": lang}
    assert a2.kuerzen(ein, punkte, r2, befund) is None and a2.letzte_kuerzung["grund"] == "Ende fehlt"
    assert r2["text"] == lang
    # zweimal zu kurz: verworfen
    a3 = sm.Ablauf(Kuerzer([zu_kurz, zu_kurz]), notizen_zuerst=True)
    assert a3.kuerzen(ein, punkte, {"text": lang}, befund) is None and a3.letzte_kuerzung["grund"] == "Länge passt nicht"
