"""Server 0.4.76, Weg „Notizen zuerst“: Auswahl über die ganze Runde, zweiter Blick weiter nach vorn, „widerspricht“
ohne Zitat verorten, Kürzen ohne Kulissen-Namen, Zahlwörter als Regelrest, kein Absatz mit nackter Zahl am Anfang."""
import json
import re

from app import artefakte
from app import sprachmodell as sm


def _notizen(minuten: int, je: int = 5) -> str:
    return "\n".join(f"[{m // 60}:{m % 60:02d}:00] Ereignis bei Minute {m}." for m in range(0, minuten, je))


def test_abschnitte_teilen_kontingent():
    a = sm.auswahl_abschnitte(_notizen(155), 155.0, 30)
    assert [x["anteil"] for x in a] == [8, 8, 7, 7] and sum(x["anteil"] for x in a) == 30
    assert a[0]["von"] == 0.0 and abs(a[-1]["bis"] - 155 * 60) < 1e-6
    # jede Notiz in genau einem Abschnitt, in der Reihenfolge der Zeit
    zeilen = [z for x in a for z in x["notizen"].splitlines()]
    assert zeilen == _notizen(155).splitlines()
    assert "Minute 150" in a[-1]["notizen"] and "Minute 150" not in a[0]["notizen"]
    # kurze Runde: ein Abschnitt; Zeilen ohne Zeit bleiben beim Abschnitt davor; leere Abschnitte geben ihren Anteil ab
    assert len(sm.auswahl_abschnitte(_notizen(50), 50.0, 12)) == 1
    b = sm.auswahl_abschnitte("[0:01:00] Anfang.\nohne Zeit\n[2:30:00] Ende.", 155.0, 30)
    assert [x["anteil"] for x in b] == [15, 15] and b[0]["notizen"].endswith("ohne Zeit")


def test_auswahl_lesen_behaelt_kritisches():
    d = {"ereignisse": [{"zeit": f"{i}:00", "ereignis": f"E{i}", "ausgang": "", "rang": "wichtig"} for i in range(1, 6)]
         + [{"zeit": "59:00", "ereignis": "Tod am Ende", "ausgang": "tot", "rang": "kritisch"}]}
    aus = sm.auswahl_lesen(d, 3)
    assert [e["ereignis"] for e in aus] == ["E1", "E2", "Tod am Ende"]


class Klient:
    modell = "medium"

    def __init__(self):
        self.aufrufe = []

    def chat(self, system, nutzer):
        self.aufrufe.append((system, nutzer))
        zeiten = re.findall(r"^\[(\d+:\d{2}:\d{2})\]", nutzer, re.M)
        anteil = int(re.search(r"höchstens (\d+)", system).group(1))
        return sm.Antwort(json.dumps({"ereignisse": [
            {"zeit": z, "ereignis": f"Ereignis {z}", "ausgang": "", "rang": "wichtig"} for z in zeiten[:anteil + 3]]}), 1, 1)


def test_auswahl_ueber_die_ganze_runde():
    k = Klient()
    a = sm.Ablauf(k)
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [],
           "transkript": [{"start": float(s), "sprecher": "A", "text": "x"} for s in range(0, 155 * 60 + 1, 60)]}
    aus = a.auswahl(ein, _notizen(155))
    assert len(k.aufrufe) == 4
    assert "Abschnitt 4 von 4 der Runde (1:56:15 bis 2:35:00)" in k.aufrufe[3][0]
    assert "Minute 150" in k.aufrufe[3][1] and "Minute 0." not in k.aufrufe[3][1]
    assert "Stand am Ende der Runde" in k.aufrufe[3][1]
    assert len(aus) == 30 and aus[-1]["zeit"] >= 116 * 60  # das letzte Drittel hat Ereignisse
    assert [e["zeit"] for e in aus] == sorted(e["zeit"] for e in aus)
    assert "{abschnitt}" not in k.aufrufe[0][0] and "Begrüßungen" in sm.SYSTEM_AUSWAHL


def test_widerspricht_ohne_zitat_verorten():
    kapitel = "Mara floh aus der Stadt.\n\nAm Tor schossen Tilo und Bren auf die Wächter, einer brach zusammen."
    punkte = [{"zeit": 1.0, "ereignis": "Mara flieht.", "ausgang": "", "rang": "wichtig"},
              {"zeit": 2.0, "ereignis": "Tilo schießt auf den Wächter am Tor.", "ausgang": "Wächter bricht zusammen",
               "rang": "kritisch"}]
    d = {"punkte": [{"nr": 1, "status": "widerspricht", "absatz": 1, "zitat": "erfunden und nicht da"},
                    {"nr": 2, "status": "widerspricht", "absatz": 2, "zitat": "Tilo schoss allein",
                     "begruendung": "Nur Tilo schoss."}]}
    aus = sm.pflicht_lesen(d, 2, 2, kapitel, "de", punkte)
    assert aus[0]["status"] == "ungeprueft"  # Absatz enthält das Ereignis nicht erkennbar
    assert aus[1]["status"] == "widerspricht" and aus[1]["absatz"] == 1 and aus[1]["zitat"] == ""
    # ein „unklar“ der Prüfung markiert nichts mehr
    assert sm.pflicht_lesen({"punkte": [{"nr": 1, "status": "unklar", "absatz": 1}]}, 1, 2, kapitel)[0]["status"] == "ungeprueft"
    p = sm.pruefansicht_aus_pflicht(kapitel, punkte, aus, [], [], "m", False)
    assert p["paragraphs"][1]["verdict"] == "contradicted" and p["paragraphs"][1]["note"] == "Nur Tilo schoss."
    befund = [{"nr": 2, "status": "unklar", "absatz": 1, "zitat": "", "begruendung": ""}]
    assert sm.pruefansicht_aus_pflicht(kapitel, punkte, befund, [], [], "m", False)["paragraphs"][1]["verdict"] == "supported"


def test_kuerzen_namen_nur_aus_der_auswahl():
    lang = "Sie kamen mit der Lady Luck an und tranken in der Stumbling Tiger Bar. Dann traf Mara den Fürsten Al Rahim."
    kurz = "Mara traf den Fürsten Al Rahim."
    assert sm._namen_fehlen(lang, kurz, ["Mara"])  # alter Weg: Kulisse blockiert
    assert sm._namen_fehlen(lang, kurz, ["Mara"], "1. [1:00] Mara trifft Al Rahim.") == []
    assert sm._namen_fehlen(lang, "Sie trafen jemanden.", ["Mara"], "1. [1:00] Mara trifft Al Rahim.") == ["mara", "rahim"]


def test_kuerzen_zu_kurz_einmal_nachfordern():
    class K:
        modell = "m"

        def __init__(self):
            self.n = 0
            self.nutzer = []

        def chat(self, system, nutzer):
            self.n += 1
            self.nutzer.append(nutzer)
            if "Ereignisse, die vorkommen müssen" in nutzer and "Kapitel:\n" in nutzer:
                laenge = 200 if self.n == 1 else 1200
                return sm.Antwort(json.dumps({"text": " ".join(["Mara ging."] * (laenge // 2))}), 1, 1)
            return sm.Antwort(json.dumps({"punkte": [{"nr": 1, "status": "erzaehlt", "absatz": 1, "zitat": "Mara ging"}]}), 1, 1)

    k = K()
    a = sm.Ablauf(k)
    ein = {"kampagne": "K", "session_nummer": 1, "personen": [{"charakter": "Mara"}],
           "transkript": [{"start": 155 * 60.0, "sprecher": "A", "text": "x"}]}
    r = {"text": " ".join(["Mara ging."] * 1100)}
    befund = [{"nr": 1, "status": "erzaehlt", "absatz": 0, "zitat": "Mara ging", "begruendung": ""}]
    neu = a.kuerzen(ein, [{"zeit": 1.0, "ereignis": "Mara geht.", "ausgang": "", "rang": "kritisch"}], r, befund)
    assert "nur 200 Wörter" in k.nutzer[1] and a.letzte_kuerzung["zu_kurz"] == 200
    assert neu is not None and a.letzte_kuerzung["angenommen"] is True and a.letzte_kuerzung["woerter_neu"] == 1200


def test_zahlwoerter_als_regelrest():
    s = ("Die Scheibe fühlte sich eiskalt an, und Rika verlor unwillkürlich zwei Magiepunkte, während er zitterte.")
    assert artefakte.regelteil(s, artefakte._REGEL) == "Die Scheibe fühlte sich eiskalt an, während er zitterte."
    assert artefakte.regelteil("Der Wächter traf ihn für drei Trefferpunkte am Arm.", artefakte._REGEL) == \
        "Der Wächter traf ihn am Arm."
    for alltag in ("Es gab drei Treffpunkte in der Stadt.", "Sie hatten zwei Standpunkte.", "Ein Höhepunkt des Abends."):
        assert artefakte.regelteil(alltag, artefakte._REGEL) == alltag
    assert sm.regelreste("Rika erschrak (zwei Magiepunkte) und ließ los.") == "Rika erschrak und ließ los."


def test_absatz_mit_nackter_zahl():
    t = sm.absaetze_teilen("Er dachte an Hafenstadt\n\n22. Die Gruppe fuhr los.\n\nDas war gut.\n\n3. Ein neuer Tag.")
    assert t == "Er dachte an Hafenstadt 22. Die Gruppe fuhr los.\n\nDas war gut.\n\n3. Ein neuer Tag."
