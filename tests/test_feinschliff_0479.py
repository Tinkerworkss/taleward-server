"""Server 0.4.79: Teile nur an Zeitsprung oder Ortswechsel und mit dem Platz der ganzen Runde, Gerüchte als Gerüchte,
Regelbegriffe des Systems und Sätze über die Runde selbst aus dem Kapitel, mehr Vorschläge bei langen Runden, keine
Vermutungen in SL-Notizen."""
from app import artefakte
from app import sprachmodell as sm
from app.namenshilfe import regelbegriffe, systembegriffe

NOTIZEN = "\n".join(f"[{m // 60}:{m % 60:02d}:00] Szene bei Minute {m}." for m in range(0, 430, 10))
DAUER = 430 * 60.0


def _schnitt(zeit, art, minute, titel="T"):
    return {"zeit": zeit, "art": art, "zitat": f"Szene bei Minute {minute}", "titel": titel}


def test_nur_zeitsprung_und_ortswechsel():
    for art in ("strangende", "pause", "end_of_thread"):
        assert sm.einschnitte_lesen({"einschnitte": [_schnitt("3:10:00", art, 190)]}, NOTIZEN, DAUER, 2) == []
    # 0.4.80: ein Ortswechsel zählt nicht mehr, auch nicht allein
    assert sm.einschnitte_lesen({"einschnitte": [_schnitt("3:10:00", "ortswechsel", 190)]}, NOTIZEN, DAUER, 2) == []
    # nur Zeitsprünge zählen
    d = {"einschnitte": [_schnitt("2:20:00", "ortswechsel", 140), _schnitt("3:10:00", "zeitsprung", 190),
                         _schnitt("5:00:00", "ortswechsel", 300)]}
    assert [t["von"] for t in sm.einschnitte_lesen(d, NOTIZEN, DAUER, 2)] == [0.0, 11400.0]
    d["einschnitte"][2]["art"] = "zeitsprung"
    assert [t["von"] for t in sm.einschnitte_lesen(d, NOTIZEN, DAUER, 2)] == [0.0, 11400.0, 18000.0]
    assert "zeitsprung" in sm.SYSTEM_EINSCHNITT and "strangende" not in sm.SYSTEM_EINSCHNITT
    assert '"ortswechsel"' not in sm.SYSTEM_EINSCHNITT


def test_teile_teilen_sich_den_platz():
    teile = [{"von": 0.0, "bis": 190 * 60.0}, {"von": 190 * 60.0, "bis": 420 * 60.0}]
    g = [sm.teil_grenzen(t, 420 * 60.0) for t in teile]
    assert g == [(810, 1090), (990, 1310)] and (sum(x[0] for x in g), sum(x[1] for x in g)) == (1800, 2400)
    assert [sm.teil_hoechstens(t, 420 * 60.0) for t in teile] == [20, 25]
    # ein kurzer Teil bekommt einen Mindestplatz
    kurz = {"von": 0.0, "bis": 30 * 60.0}
    assert sm.teil_grenzen(kurz, 420 * 60.0) == (300, 500) and sm.teil_hoechstens(kurz, 420 * 60.0) == 12


def test_geruechte_in_allen_anweisungen():
    for anweisung in (sm.SYSTEM_NOTIZEN, sm.SYSTEM_AUSWAHL, sm.SYSTEM_RECAP, sm.SYSTEM_VORSCHLAEGE):
        assert "„soll … haben“" in anweisung
    assert "Bahnhof" in sm.SYSTEM_VORSCHLAEGE and "quest" in sm.SYSTEM_VORSCHLAEGE


def test_regelbegriffe_des_systems():
    assert regelbegriffe("shadowrun") == ("Edge",) and "Edge" in systembegriffe("shadowrun")
    assert regelbegriffe("other") == () and regelbegriffe(None) == () and regelbegriffe("gibt-es-nicht") == ()
    text = ("Mit einem Lächeln und einem Hauch von Edge überredete Ana den Fahrer, die Fahrt zu schenken. Bo nickte, "
            "während Ana überlegte, Edge für den ersten Eindruck einzusetzen. Der Rand der Klinge war scharf.")
    aus, befunde = artefakte.kapitel(text, [], [], regelbegriffe("shadowrun"))
    assert aus == "Mit einem Lächeln überredete Ana den Fahrer, die Fahrt zu schenken. Bo nickte. Der Rand der Klinge war scharf."
    assert [b["art"] for b in befunde] == ["regel", "regel"]
    # ohne System bleibt der Text, wie er war (der Rand heißt nicht Edge)
    assert artefakte.kapitel(text, [], [])[0] == text
    assert sm._regelwoerter({"system": "shadowrun"}) == ("Edge",) and sm._regelwoerter({}) == ()


def test_saetze_ueber_die_runde_fallen_weg():
    text = ("Ana beendete die Session in der Matrix und rief Bo an. Die Runde blieb bei Planung und Rollenspiel, ohne "
            "dass es zu einem Kampf kam. Die nächste Session sollte in der Garage beginnen. Bo lachte.")
    aus, befunde = artefakte.kapitel(text, [], [])
    assert aus == "Ana beendete die Session in der Matrix und rief Bo an. Bo lachte."
    assert [b["art"] for b in befunde] == ["tischmeta", "tischmeta"]


def test_mehr_vorschlaege_bei_langen_runden():
    kurz = {"transkript": [{"start": 120 * 60.0}]}
    lang = {"transkript": [{"start": 300 * 60.0}]}
    assert sm.vorschlaege_hoechstens(kurz) == 15 and sm.vorschlaege_hoechstens(lang) == 20
    roh = [{"entryType": "npc", "action": "create", "title": f"Figur {i}", "detail": f"Figur {i} lebt am Hafen."}
           for i in range(25)]
    assert len(sm.pruefen(roh, set(), set())) == 15 and len(sm.pruefen(roh, set(), set(), hoechstens=20)) == 20


def test_keine_vermutungen_in_sl_notizen():
    roh = [{"entryType": "location", "action": "create", "title": "Die alte Tankstelle",
            "detail": "Hauptquartier der Gang. Ana sagt: „Da könnte jemand schießen.“",
            "gmNotes": "Die Gang zahlt Schutzgeld. Die Tankstelle könnte geheime Räume haben."}]
    v = sm.pruefen(roh, set(), set())[0]
    assert v["gmNotes"] == "Die Gang zahlt Schutzgeld." and "könnte" in v["detail"]
    nur_vermutung = [dict(roh[0], gmNotes="Sie könnte ein Treffpunkt sein.")]
    assert sm.pruefen(nur_vermutung, set(), set())[0]["gmNotes"] is None
